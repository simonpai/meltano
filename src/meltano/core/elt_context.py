from typing import Optional
from collections import namedtuple

from meltano.core.project import Project
from meltano.core.job import Job
from meltano.core.config_service import ConfigService
from meltano.core.plugin import Plugin, PluginType, PluginRef
from meltano.core.plugin.settings_service import PluginSettingsService
from meltano.core.plugin_discovery_service import PluginDiscoveryService
from meltano.core.plugin_invoker import invoker_factory
from meltano.core.utils import flatten


class PluginContext(
    namedtuple("PluginContext", "ref install definition settings_service session")
):
    @property
    def namespace(self):
        return self.definition.namespace

    @property
    def name(self):
        return self.install.name

    @property
    def type(self):
        return self.ref.type

    def get_config(self, name, **kwargs):
        return self.settings_service.get(name, session=self.session, **kwargs)

    def config_dict(self, **kwargs):
        return self.settings_service.as_dict(session=self.session, **kwargs)

    def config_env(self, **kwargs):
        return self.settings_service.as_env(session=self.session, **kwargs)


class ELTContext:
    def __init__(
        self,
        project,
        job: Optional[Job] = None,
        extractor: Optional[PluginContext] = None,
        loader: Optional[PluginContext] = None,
        transformer: Optional[PluginContext] = None,
        dry_run: Optional[bool] = False,
        full_refresh: Optional[bool] = False,
        select_filter: Optional[list] = [],
        plugin_discovery_service: PluginDiscoveryService = None,
    ):
        self.project = project
        self.job = job
        self.extractor = extractor
        self.loader = loader
        self.transformer = transformer
        self.dry_run = dry_run
        self.full_refresh = full_refresh
        self.select_filter = select_filter

        self.plugin_discovery_service = (
            plugin_discovery_service or PluginDiscoveryService(project)
        )

    @property
    def elt_run_dir(self):
        if self.job:
            return self.project.job_dir(self.job.job_id, str(self.job.run_id))

    def extractor_invoker(self):
        return invoker_factory(
            self.project,
            self.extractor.install,
            run_dir=self.elt_run_dir,
            plugin_settings_service=self.extractor.settings_service,
            plugin_discovery_service=self.plugin_discovery_service,
        )

    def loader_invoker(self):
        return invoker_factory(
            self.project,
            self.loader.install,
            run_dir=self.elt_run_dir,
            plugin_settings_service=self.loader.settings_service,
            plugin_discovery_service=self.plugin_discovery_service,
        )

    def transformer_invoker(self):
        return invoker_factory(
            self.project,
            self.transformer.install,
            plugin_settings_service=self.transformer.settings_service,
            plugin_discovery_service=self.plugin_discovery_service,
        )


class ELTContextBuilder:
    def __init__(
        self,
        project: Project,
        config_service: ConfigService = None,
        plugin_discovery_service: PluginDiscoveryService = None,
    ):
        self.project = project
        self.config_service = config_service or ConfigService(project)
        self.plugin_discovery_service = (
            plugin_discovery_service
            or PluginDiscoveryService(project, config_service=config_service)
        )

        self._extractor = None
        self._loader = None
        self._transformer = None
        self._job = None
        self._dry_run = False
        self._full_refresh = False
        self._select_filter = None

    def with_extractor(self, extractor_name: str):
        self._extractor = PluginRef(PluginType.EXTRACTORS, extractor_name)

        return self

    def with_loader(self, loader_name: str):
        self._loader = PluginRef(PluginType.LOADERS, loader_name)

        return self

    def with_transform(self, transform: str):
        if transform == "skip":
            return self

        return self.with_transformer("dbt")

    def with_transformer(self, transformer_name: str):
        self._transformer = PluginRef(PluginType.TRANSFORMERS, transformer_name)

        return self

    def with_job(self, job: Job):
        self._job = job

        return self

    def with_dry_run(self, dry_run):
        self._dry_run = dry_run

        return self

    def with_full_refresh(self, full_refresh):
        self._full_refresh = full_refresh

        return self

    def with_select_filter(self, select_filter):
        self._select_filter = select_filter

        return self

    def plugin_context(self, session, plugin: PluginRef, env={}, config={}):
        return PluginContext(
            ref=plugin,
            install=self.config_service.find_plugin(
                plugin_name=plugin.name, plugin_type=plugin.type
            ),
            definition=self.plugin_discovery_service.find_plugin(
                plugin_name=plugin.name, plugin_type=plugin.type
            ),
            settings_service=PluginSettingsService(
                self.project,
                plugin,
                config_service=self.config_service,
                plugin_discovery_service=self.plugin_discovery_service,
                env_override=env,
                config_override=config,
            ),
            session=session,
        )

    def context(self, session) -> ELTContext:
        def env_for_plugin(plugin):
            env_struct = {
                "meltano": {
                    plugin.type.singular: {  # MELTANO_EXTRACTOR_...
                        "name": plugin.name,
                        "namespace": plugin.namespace,
                    }
                },
                **plugin.config_env(extras=False),
            }
            env_vars = flatten(env_struct, "env_var")

            return {k: str(v) for k, v in env_vars.items() if v is not None}

        env = {}

        extractor = None
        if self._extractor:
            config = {}
            if self._select_filter:
                config["_select_filter"] = self._select_filter

            extractor = self.plugin_context(session, self._extractor, config=config)

            env.update(env_for_plugin(extractor))

        loader = None
        if self._loader:
            loader = self.plugin_context(session, self._loader, env=env.copy())

            env.update(env_for_plugin(loader))

        transformer = None
        if self._transformer:
            transformer = self.plugin_context(
                session, self._transformer, env=env.copy()
            )

        return ELTContext(
            self.project,
            job=self._job,
            extractor=extractor,
            loader=loader,
            transformer=transformer,
            dry_run=self._dry_run,
            full_refresh=self._full_refresh,
            select_filter=self._select_filter,
            plugin_discovery_service=self.plugin_discovery_service,
        )
