import argparse
import atexit
import datetime
import functools
import logging

from semarathon.persistence import load_jobs, save_jobs, save_jobs_job
from semarathon.utils import load_text


def start_bot(bot_system):
    logging.info("Starting bot")
    bot_system.job_queue.run_repeating(
        callback=save_jobs_job, interval=datetime.timedelta(minutes=1)
    )
    try:
        load_jobs(bot_system.job_queue)
    except FileNotFoundError:
        pass

    bot_system.updater.start_polling()


def shutdown_bot(bot_system):
    logging.info("Shutting down bot")
    bot_system.updater.stop()
    sessions = bot_system.sessions
    for chat_id, session in sessions.copy().items():
        session.send_message("*SERVER SHUTDOWN* – Going to sleep with the fishes...")
        del sessions[chat_id]
    save_jobs(bot_system.job_queue)


def main(logging_level=logging.INFO):
    # noinspection SpellCheckingInspection
    logging.basicConfig(
        format="%(asctime)s - %(name)s:%(levelname)s - %(message)s", level=logging_level
    )

    logging.info("Initializing semarathon.bot module")
    from semarathon.bot import SEMarathonBotSystem

    logging.info("Initializing bot system")
    se_marathon_bot_system = SEMarathonBotSystem(load_text("token"))

    logging.info("Starting bot")
    atexit.register(functools.partial(shutdown_bot, se_marathon_bot_system))
    start_bot(se_marathon_bot_system)
    logging.info("Bot online")

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
