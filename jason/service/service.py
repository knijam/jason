import threading
from typing import Any, Type

import waitress

from .app import App
from .config import ServiceConfig


class Service:
    def __init__(self, config_class: Type[ServiceConfig], _app_gen: Any = App):
        self._app_gen = _app_gen
        self._config_class = config_class
        self._app = None
        self._config = None
        self._debug = False
        self._callback = None

    def _serve(self, host, port):
        if self._debug:
            self._app.run(host=host, port=port)
        else:
            waitress.serve(self._app, host=host, port=port)

    def _pre_command(self, debug, config_values):
        self._debug = debug
        self._config = self._config_class.load(**config_values)
        self._app = self._app_gen(__name__, config=self._config, testing=self._debug)
        self._set_up(self._app)

    def _set_up(self, app):
        raise NotImplementedError

    def run(self, debug=False, no_serve=False, detach=False, **config_values):
        self._pre_command(debug, config_values)
        if "service_threads" in self._app.extensions:
            service_threads = self._app.extensions["service_threads"]
            service_threads.run_all(threaded=False)
        if no_serve is False and self._config.SERVE is True:
            if not detach:
                self._serve(host=self._config.SERVE_HOST, port=self._config.SERVE_PORT)
            else:
                thread = threading.Thread(
                    target=self._serve,
                    kwargs={
                        "host": self._config.SERVE_HOST,
                        "port": self._config.SERVE_PORT,
                    },
                    daemon=True,
                )
                thread.start()
        else:
            while threading.active_count() - 1:
                ...

    def test_app(self, **config_values):
        self.run(debug=True, no_serve=True, **config_values)
        return self._app

    def app(self, debug=False, **config_values):
        self.run(debug=debug, no_serve=True, **config_values)
        return self._app

    def config(self, debug=False, **config_values):
        self._pre_command(debug, config_values)
        prop_strings = (
            f"{key}={value}" for key, value in self._config.__dict__.items()
        )
        return "\n".join(prop_strings)

    def extensions(self, debug=False, **config_values):
        self._pre_command(debug, config_values)
        return "\n".join(e for e in self._app.extensions)

    def __call__(self, func):
        self._set_up = func
        return self
