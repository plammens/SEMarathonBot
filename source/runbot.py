import atexit
import datetime
import functools
import logging

if __name__ == "__main__":
    # noinspection SpellCheckingInspection
    logging.basicConfig(
        format="%(asctime)s - %(name)s:%(levelname)s - %(message)s", level=logging.DEBUG
    )
    logging.info("Initializing bot")

from semarathon.utils import load_text
from semarathon.bot import SEMarathonBotSystem
from semarathon.persistence import load_jobs, save_jobs, save_jobs_job


def start_bot(bot_system: SEMarathonBotSystem):
    logging.info("Starting bot")
    bot_system.job_queue.run_repeating(
        callback=save_jobs_job, interval=datetime.timedelta(minutes=1)
    )
    try:
        load_jobs(bot_system.job_queue)
    except FileNotFoundError:
        pass

    bot_system.updater.start_polling()


def shutdown_bot(bot_system: SEMarathonBotSystem):
    logging.info("Shutting down bot")
    bot_system.updater.stop()
    sessions = bot_system.sessions
    for chat_id, session in sessions.copy().items():
        session.send_message("*SERVER SHUTDOWN* – Going to sleep with the fishes...")
        del sessions[chat_id]
    save_jobs(bot_system.job_queue)


if __name__ == "__main__":
    se_marathon_bot_system = SEMarathonBotSystem(load_text("token"))
    atexit.register(functools.partial(shutdown_bot, se_marathon_bot_system))
    start_bot(se_marathon_bot_system)

    # run bot until interrupted:
    se_marathon_bot_system.updater.idle()

    shutdown_bot(se_marathon_bot_system)
