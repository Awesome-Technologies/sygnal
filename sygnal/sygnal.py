# -*- coding: utf-8 -*-
# Copyright 2014 OpenMarket Ltd
# Copyright 2018, 2019 New Vector Ltd
# Copyright 2019 The Matrix.org Foundation C.I.C.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import copy
import importlib
import logging
import os
import sys
from logging.handlers import WatchedFileHandler

import opentracing
import prometheus_client
import twisted.internet.reactor
import yaml
from opentracing.scope_managers.asyncio import AsyncioScopeManager
from twisted.internet import asyncioreactor
from twisted.internet.defer import ensureDeferred

from sygnal.http import PushGatewayApiServer
from sygnal.utils import collect_all_deferreds
from .database import Database

# we remove the global reactor to make it evident when it has accidentally
# been used:
twisted.internet.reactor = None

logger = logging.getLogger(__name__)

CONFIG_DEFAULTS = {
    "http": {"port": 5000, "bind_addresses": ["127.0.0.1"]},
    "log": {"level": "info", "file": ""},
    "db": {"dbfile": "sygnal.db"},
    "metrics": {
        "prometheus": {"enabled": False, "address": "127.0.0.1", "port": 8000},
        "opentracing": {
            "enabled": False,
            "implementation": None,
            "jaeger": {},
            "service_name": "sygnal",
        },
    },
    "apps": {},
}


class Sygnal(object):
    def __init__(self, config, custom_reactor, tracer=opentracing.tracer):
        """
        Object that holds state for the entirety of a Sygnal instance.
        Args:
            config (dict): Configuration for this Sygnal
            custom_reactor: a Twisted Reactor to use.
            tracer (optional): an OpenTracing tracer. The default is the no-op tracer.
        """
        self.config = config
        self.reactor = custom_reactor
        self.pushkins = {}
        self.tracer = tracer

    def _setup(self):
        cfg = self.config

        logging.getLogger().setLevel(getattr(logging, cfg["log"]["level"].upper()))
        logfile = cfg["log"]["file"]

        if logfile != "":
            handler = WatchedFileHandler(logfile)
            formatter = logging.Formatter(
                "%(asctime)s [%(process)d] %(levelname)-5s "
                "%%(request_id)s %(name)s %(message)s"
            )
            handler.setFormatter(formatter)
            logging.getLogger().addHandler(handler)
        else:
            logging.basicConfig()

        # TODO if cfg.has_option("metrics", "sentry_dsn"):
        #     # Only import sentry if enabled
        #     import sentry_sdk
        #     from sentry_sdk.integrations.flask import FlaskIntegration
        #     sentry_sdk.init(
        #         dsn=cfg.get("metrics", "sentry_dsn"),
        #         integrations=[FlaskIntegration()],
        #     )

        promcfg = config["metrics"]["prometheus"]
        if promcfg["enabled"] is True:
            prom_addr = promcfg["address"]
            prom_port = int(promcfg["port"])
            logging.info(
                "Starting Prometheus Server on %s port %d", prom_addr, prom_port
            )

            prometheus_client.start_http_server(port=prom_port, addr=prom_addr or "")

        tracecfg = config["metrics"]["opentracing"]
        if tracecfg["enabled"] is True:
            if tracecfg["implementation"] == "jaeger":
                try:
                    import jaeger_client

                    jaeger_cfg = jaeger_client.Config(
                        config=tracecfg["jaeger"],
                        service_name=tracecfg["service_name"],
                        scope_manager=AsyncioScopeManager(),
                    )

                    sygnal.tracer = jaeger_cfg.initialize_tracer()

                    logging.info("Enabled OpenTracing support with Jaeger")
                except ModuleNotFoundError:
                    logger.critical(
                        "You have asked for OpenTracing with Jaeger but do not have"
                        " the Python package 'jaeger_client' installed."
                    )
                    raise
            else:
                logger.error(
                    "Unknown OpenTracing implementation: %s.", tracecfg["impl"]
                )
                sys.exit(1)

        self.database = Database(cfg["db"]["dbfile"], self.reactor)

        for app_id, app_cfg in cfg["apps"].items():
            try:
                self.pushkins[app_id] = self._make_pushkin(app_id, app_cfg)
            except Exception:
                logger.exception(
                    "Failed to load and create pushkin for kind %s", app_cfg["type"]
                )
                raise

        if len(self.pushkins) == 0:
            logger.error("No app IDs are configured. Edit sygnal.yaml to define some.")
            sys.exit(1)

        logger.info("Configured with app IDs: %r", self.pushkins.keys())
        logger.info("Setup completed")

    def _make_pushkin(self, app_name, app_config):
        app_type = app_config["type"]
        if "." in app_type:
            kind_split = app_type.rsplit(".", 1)
            to_import = kind_split[0]
            to_construct = kind_split[1]
        else:
            to_import = f"sygnal.{app_type}pushkin"
            to_construct = f"{app_type.capitalize()}Pushkin"

        logger.info("Importing pushkin module: %s", to_import)
        pushkin_module = importlib.import_module(to_import)
        logger.info("Creating pushkin: %s", to_construct)
        clarse = getattr(pushkin_module, to_construct)
        return clarse(app_name, self, app_config)

    def run(self):
        self._setup()
        port = int(self.config["http"]["port"])
        bind_addresses = self.config["http"]["bind_addresses"]
        pushgateway_api = PushGatewayApiServer(self)

        start_deferred = collect_all_deferreds(
            [ensureDeferred(pushkin.start(self)) for pushkin in self.pushkins.values()]
        )

        exit_code = 0

        def on_started(_):
            for interface in bind_addresses:
                logger.info("Starting listening on %s port %d", interface, port)
                self.reactor.listenTCP(port, pushgateway_api.site, interface=interface)

        def on_failed_to_start(failure):
            nonlocal exit_code
            exit_code = 1
            logger.error("Failed to start due to exception: %s", failure)
            self.reactor.callLater(0, self.reactor.stop)

        start_deferred.addCallback(on_started)
        start_deferred.addErrback(on_failed_to_start)

        logger.info("Starting pushkins")
        self.reactor.run()

        sys.exit(exit_code)

    def shutdown(self):
        pass  # TODO


def parse_config():
    config_path = os.getenv("SYGNAL_CONF", "sygnal.yaml")
    with open(config_path) as file_handle:
        return yaml.safe_load(file_handle)


def check_config(config):
    UNDERSTOOD_CONFIG_FIELDS = CONFIG_DEFAULTS.keys()

    def check_section(section_name, known_keys):
        nonunderstood = set(config[section_name].keys()).difference(known_keys)
        if len(nonunderstood) > 0:
            logger.warning(
                f"The following configuration fields in '{section_name}' are not understood: %s",
                nonunderstood,
            )

    nonunderstood = set(config.keys()).difference(UNDERSTOOD_CONFIG_FIELDS)
    if len(nonunderstood) > 0:
        logger.warning(
            "The following configuration fields are not understood: %s", nonunderstood
        )

    check_section("http", {"port", "bind_addresses"})
    check_section("log", {"file", "level"})
    check_section("db", {"dbfile"})


def merge_left_with_defaults(defaults, loaded_config):
    result = defaults.copy()

    # copy defaults or override them
    for k, v in result.items():
        if isinstance(v, dict):
            if k in loaded_config:
                result[k] = merge_left_with_defaults(v, loaded_config[k])
            else:
                result[k] = copy.deepcopy(v)
        elif k in loaded_config:
            result[k] = loaded_config[k]

    # copy things with no defaults
    for k, v in loaded_config.items():
        if k not in result:
            result[k] = v

    return result


if __name__ == "__main__":
    config = parse_config()
    config = merge_left_with_defaults(CONFIG_DEFAULTS, config)
    check_config(config)
    sygnal = Sygnal(config, custom_reactor=asyncioreactor.AsyncioSelectorReactor())
    sygnal.run()
