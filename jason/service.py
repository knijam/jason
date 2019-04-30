import threading
from typing import Any, Type

import flask
import kombu
import waitress

from jason import mixins, props


class ServiceConfig(props.Config):
    SERVE = props.Bool(default=True)
    SERVE_HOST = props.String(default="localhost")
    SERVE_PORT = props.Int(default=5000)


class Consumer:
    def __init__(self, app=None, **kwargs):
        self.app = None
        self.host = None
        self.port = None
        self.username = None
        self.password = None
        self.backend = None
        self.init_app(app)
        self.configure(**kwargs)

    def init_app(self, app):
        if not app:
            return
        self.app = app
        self.app.before_first_request(self.run)
        self.app.extensions["consumer"] = self

    def configure(self, backend, host=None, port=None, username=None, password=None):
        if host is not None:
            self.host = host
        if port is not None:
            self.port = port
        if username is not None:
            self.username = username
        if password is not None:
            self.password = password
        if backend is not None:
            self.backend = backend

    def _main(self):
        with self.create_connection() as connection, self.create_consumer(connection):
            while True:
                connection.drain_events()

    def run(self, threaded=True):
        if not threaded:
            self._main()
        else:
            thread = threading.Thread(target=self._main)
            thread.start()

    def create_connection(self):
        return kombu.Connection(
            transport=self.backend,
            host=self.host,
            port=self.port,
            user_id=self.username,
            password=self.password,
        )

    def create_consumer(self, connection):
        raise NotImplementedError


class App(flask.Flask):
    def __init__(self, name: str, config: Any, testing: bool = False, **kwargs: Any):
        super(App, self).__init__(name, **kwargs)
        config.update(self.config)
        self.config = config
        self.testing = testing

    def init_sqlalchemy(self, database, migrate=None):
        self._assert_mixin(self.config, mixins.PostgresConfigMixin, "database")
        self.config.SQLALCHEMY_DATABASE_URI = self._database_uri()
        self.config.SQLALCHEMY_TRACK_MODIFICATIONS = False
        database.init_app(app=self)
        if migrate:
            migrate.init_app(app=self, db=database)

    def init_redis(self, cache):
        self._assert_mixin(self.config, mixins.RedisConfigMixin, "cache")
        cache.init_app(
            app=self,
            host=self.config.REDIS_HOST,
            port=self.config.REDIS_PORT,
            password=self.config.REDIS_PASS,
        )

    def init_celery(self, celery):
        self._assert_mixin(self.config, mixins.CeleryConfigMixin, "celery")

        def backend_url(backend):
            if backend == "rabbitmq":
                self._assert_mixin(
                    self.config,
                    mixins.RabbitConfigMixin,
                    "celery broker",
                    "if broker backend is rabbitmq",
                )
                return self._rabbit_uri()
            elif backend == "redis":
                self._assert_mixin(
                    self.config,
                    mixins.RedisConfigMixin,
                    "celery broker",
                    "if broker backend is redis",
                )
                return self._redis_uri(database_id=self.config.CELERY_REDIS_DATABASE_ID)
            raise ValueError(f"invalid backend name '{backend}'")

        celery.conf.broker_url = backend_url(self.config.CELERY_BROKER_BACKEND)
        celery.conf.result_backend = backend_url(self.config.CELERY_RESULTS_BACKEND)
        task_base = celery.Task

        class AppContextTask(task_base):
            abstract = True

            def __call__(self, *args, **kwargs):
                with self.app_context():
                    return task_base.__call__(self, *args, **kwargs)

        # noinspection PyPropertyAccess
        celery.Task = AppContextTask
        celery.finalize()

    def init_consumer(self, consumer):
        self._assert_mixin(self.config, mixins.RabbitConfigMixin, "consumer")
        consumer.init_app(
            app=self,
            host=self.config.RABBIT_HOST,
            port=self.config.RABBIT_PORT,
            username=self.config.RABBIT_USER,
            password=self.config.RABBIT_PASS,
        )

    @staticmethod
    def _assert_mixin(config, mixin, item, condition=""):
        if not isinstance(config, mixin):
            raise TypeError(
                f"could not initialise {item}. "
                f"config must sub-class {mixin.__name__} {condition}"
            )

    def _redis_uri(self, database_id: int = None):
        if self.config.REDIS_PASS is not None:
            credentials = f":{self.config.REDIS_PASS}@"
        else:
            credentials = None
        uri = f"{self.config.REDIS_DRIVER}://{credentials}{self.config.REDIS_HOST}:{self.config.REDIS_PORT}"
        if database_id is not None:
            uri += f"/{database_id}"
        return uri

    def _rabbit_uri(self):
        return (
            f"{self.config.RABBIT_DRIVER}://"
            f"{self.config.RABBIT_USER}:{self.config.RABBIT_PORT}"
            f"@{self.config.RABBIT_HOST}:{self.config.RABBIT_PORT}"
        )

    def _database_uri(self):
        if self.testing:
            return self.config.TEST_DB_URL
        if self.config.DB_USER:
            credentials = f"{self.config.DB_USER}:{self.config.DB_PASS}@"
        else:
            credentials = ""
        return f"{self.config.DB_DRIVER}://{credentials}{self.config.DB_HOST}:{self.config.DB_PORT}"


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
        if self._callback:
            self._callback(self._app, debug)

    def run(self, debug=False, no_serve=False, detach=False, **config_values):
        self._pre_command(debug, config_values)
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
        elif "consumer" in self._app.extensions:
            consumer = self._app.extensions["consumer"]
            consumer.run(threaded=False)

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
        self._callback = func
        return self


service = Service
