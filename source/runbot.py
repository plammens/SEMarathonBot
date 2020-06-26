import argparse
import atexit
import functools
import logging
import re

from semarathon.utils import Text


def start_bot(bot_system):
    logging.info("Starting bot")
    atexit.register(functools.partial(shutdown_bot, bot_system))
    bot_system.updater.start_polling()
    logging.info("Bot online")


def shutdown_bot(bot_system):
    logging.info("Shutting down bot")
    bot_system.updater.stop()
    sessions = bot_system.sessions
    for chat_id, session in sessions.copy().items():
        session.send_message("*SERVER SHUTDOWN* – Going to sleep with the fishes...")
        del sessions[chat_id]


def setup_logging(level):
    class CustomLogRecord(logging.LogRecord):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.origin = f"{self.name} ({self.threadName})"

    class BotUpdaterFilter(logging.Filter):
        PATT = re.compile(r"Bot:\d+:updater")

        def filter(self, record: logging.LogRecord) -> int:
            if record.threadName is None:
                return True
            else:
                return not self.PATT.match(record.threadName)

    logging.setLogRecordFactory(CustomLogRecord)
    # noinspection SpellCheckingInspection,PyArgumentList

    root = logging.getLogger()
    root.setLevel(level)
    # noinspection PyArgumentList
    formatter = logging.Formatter(
        fmt="{asctime} - {levelname:8} - {origin:50} - {message}", style="{",
    )
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    handler.addFilter(BotUpdaterFilter())
    root.addHandler(handler)


def construct_bot():
    logging.info("Initializing semarathon.bot module")
    from semarathon.bot import SEMarathonBotSystem

    logging.info("Initializing bot system")
    return SEMarathonBotSystem(Text.load("token"))


def main(logging_level=logging.INFO):
    setup_logging(logging_level)
    se_marathon_bot_system = construct_bot()
    start_bot(se_marathon_bot_system)
    # run bot until interrupted:
    se_marathon_bot_system.updater.idle()
    shutdown_bot(se_marathon_bot_system)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run the server for @SEMarathonBot on Telegram"
    )
    parser.add_argument("--logging-level", "-l", action="store", default=logging.INFO)

    namespace = parser.parse_args()
    main(namespace.logging_level)
