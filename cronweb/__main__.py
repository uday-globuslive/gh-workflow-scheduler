import logging
import os

from waitress import serve

from . import create_app


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = create_app()
    cfg = app.extensions["cronweb"].config
    logging.getLogger("cronweb").info("Serving on http://%s:%s", cfg.host, cfg.port)
    serve(app, host=cfg.host, port=cfg.port, threads=8)


if __name__ == "__main__":
    main()
