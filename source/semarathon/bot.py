import logging

# TODO: extract script for running bot

# noinspection SpellCheckingInspection
logging.basicConfig(format='%(asctime)s - %(name)s:%(levelname)s - %(message)s',
                    level=logging.INFO)

if __name__ == '__main__':
    logging.info("Initializing module")

import atexit
import datetime
from enum import Enum
from typing import Dict, Callable, Optional, Union

import telegram as tg
import telegram.ext as tge
from telegram.parsemode import ParseMode

from semarathon import marathon as mth
from semarathon.persistence import *
from semarathon.utils import *


# Construct bot objects:
TOKEN = load_text('token')
UPDATER = tge.Updater(token=TOKEN, use_context=True)
BOT, DISPATCHER, JOB_QUEUE = UPDATER.bot, UPDATER.dispatcher, UPDATER.job_queue

# Load text files:
USAGE_ERROR_TXT = load_text('usage-error')
INTERNAL_ERROR_TXT = load_text('internal-error')
INFO_TXT = load_text('info')
START_TXT = load_text('start')
MARATHON_NOT_CREATED_TXT = load_text('marathon_not_created')


# --------------------------------------- Helpers  ---------------------------------------

class UsageError(Exception):
    help_txt: str

    def __init__(self, *args, help_txt: str = None):
        super(UsageError, self).__init__(*args)
        self.help_txt = help_txt or "See /info for usage info"


class ArgValueError(UsageError, ValueError):
    pass


class ArgCountError(UsageError):
    pass


class OngoingOperation(Enum):
    START_MARATHON = "start marathon"
    SHUTDOWN = "shutdown"


def get_session(context: tge.CallbackContext):
    try:
        return context.chat_data['session']
    except KeyError:
        raise UsageError("Session not initialized",
                         help_txt="You must use /start before using other commands")


# --------------------------------------- Decorators  ---------------------------------------

# type alias for command handler callbacks
CommandCallbackType = Callable[[tg.Update, tge.CallbackContext], None]
CommandMethodCallbackType = Callable[['BotSession', tg.Update], None]


def cmdhandler(command: str = None, *, method: bool = True, pass_update: bool = True,
               pass_context: bool = None, **handler_kwargs) \
        -> Callable[[Union[CommandCallbackType, CommandMethodCallbackType]], CommandCallbackType]:
    """
    Decorator factory for command handlers. The returned decorator adds
    the decorated function as a command handler for the command ``command``
    to the global DISPATCHER. If ``command`` is not specified it defaults to
    the decorated function's name.

    The callback is also decorated with an exception handler before
    constructing the command handler.

    :param command: name of bot command to add a handler for
    :param method: whether the callback is in the form of a BotSession method
    :param pass_update: whether to pass the tg.Update to the callback
    :param pass_context: whether to pass the tge.CallbackContext to the callback
                         (defaults to ``not method``)
    :param handler_kwargs: additional keyword arguments for the
                           creation of the command handler (these will be passed
                           to ``telegram.ext.dispatcher.add_handler``)
    :return: the decorated function, unchanged
    """

    pass_context = pass_context if pass_context is not None else not method

    # Actual decorator
    def decorator(callback: CommandCallbackType) -> CommandCallbackType:
        command_ = command or callback.__name__

        def decorated(update: tg.Update, context: tge.CallbackContext):
            command_info = f'/{command_}@{update.effective_chat.id}'
            logging.info(f'reached {command_info}')
            try:
                # Build arguments list:
                args = []
                if method: args.append(get_session(context))
                if pass_update: args.append(update)
                if pass_context: args.append(context)

                # Actual call:
                callback(*args)

                logging.info(f'served {command_info}')
            except (UsageError, ValueError, mth.SEMarathonError) as e:
                text = '\n\n'.join([USAGE_ERROR_TXT, format_exception_md(e),
                                    getattr(e, 'help_txt', "See /info for usage info")])
                markdown_safe_reply(update.message, text)
                logging.info(f'served {command_info} (with usage/algorithm error)')
            except Exception as e:
                text = '\n\n'.join([INTERNAL_ERROR_TXT, format_exception_md(e)])
                markdown_safe_reply(update.message, text)
                logging.error(f'{command_info}: unexpected exception', exc_info=e)
            finally:
                logging.debug(f'exiting {command_info}')

        handler = tge.CommandHandler(command_, decorated, **handler_kwargs)
        DISPATCHER.add_handler(handler)
        return decorated

    return decorator


def job_callback(pass_session: bool = True, pass_bot: bool = False) -> callable:
    """Returns specialized decorator for Job callback functions"""

    def decorator(callback: callable) -> callable:
        """Actual decorator"""

        def decorated(bot: tg.Bot, job: tge.Job, *args, **kwargs):
            chat_id = job.context
            session = BotSession.SESSIONS.get(chat_id, None)
            if pass_session and session is None:
                logging.debug(f"Skipping job {callback.__name__}: "
                              f"no active session for {chat_id} found")
                return

            effective_args = []
            if pass_session: effective_args.append(session)
            if pass_bot: effective_args.append(bot)
            effective_args.extend(args)

            return callback(*effective_args, **kwargs)

        return decorated

    return decorator


def marathon_method(method: callable) -> callable:
    def decorated_method(session: 'BotSession', *args, **kwargs):
        session.check_marathon_created()
        method(session, *args, **kwargs)

    decorated_method.__name__ = method.__name__
    return decorated_method


def running_marathon_method(method: callable) -> callable:
    def decorated_method(session: 'BotSession', *args, **kwargs):
        session.check_marathon_created()
        session.check_marathon_running()
        method(session, *args, **kwargs)

    decorated_method.__name__ = method.__name__
    return decorated_method


def ongoing_operation_method(method: callable) -> callable:
    def decorated_method(session: 'BotSession', *args, **kwargs):
        session.check_operation_ongoing()
        method(session, *args, **kwargs)
        session.operation = None

    decorated_method.__name__ = method.__name__
    return decorated_method


# --------------------------------------- BotSession  ---------------------------------------

class BotSession:
    id: int
    marathon: Optional[mth.Marathon]
    operation: Optional[OngoingOperation]

    SESSIONS: Dict[int, 'BotSession'] = {}

    def __init__(self, chat_id: int):
        BotSession.SESSIONS[chat_id] = self
        self.id = chat_id
        self.marathon = None
        self.operation = None

    # ---------------------------------- Command handlers  ----------------------------------

    @staticmethod
    @cmdhandler(method=False, pass_context=False)
    def info(update: tg.Update):
        """Show info message"""
        update.message.reply_markdown(INFO_TXT)

    @staticmethod
    @cmdhandler(method=False)
    def start(update: tg.Update, context: tge.CallbackContext):
        """Start session"""
        context.chat_data['session'] = BotSession(update.message.chat_id)
        update.message.reply_text(text=START_TXT)

    @cmdhandler()
    def shutdown(self, update: tg.Update):
        self.operation = OngoingOperation.SHUTDOWN
        update.message.reply_text("Are you sure? /yes \t /no")

    @cmdhandler(pass_update=False)
    @ongoing_operation_method
    def yes(self):
        # TODO: refactor into polymorphism
        if self.operation is OngoingOperation.START_MARATHON:
            self._start_marathon()
        elif self.operation is OngoingOperation.SHUTDOWN:
            self._shutdown()

    @cmdhandler()
    @ongoing_operation_method
    def no(self, update: tg.Update):
        self.cancel(update)

    @cmdhandler()
    @ongoing_operation_method
    def cancel(self, update: tg.Update):
        update.message.reply_text("Cancelled the operation '{}'".format(self.operation.value))

    @cmdhandler()
    def new_marathon(self, update: tg.Update):
        """Create new marathon"""
        self.marathon = mth.Marathon()
        with open('text/new_marathon.txt') as text:
            update.message.reply_markdown(text=text.read().strip())

    @cmdhandler()
    @marathon_method
    def settings(self, update: tg.Update):
        """Show settings"""
        update.message.reply_markdown(text=self._settings_text())

    @cmdhandler(pass_context=True)
    @marathon_method
    def set_sites(self, update: tg.Update, context: tge.CallbackContext):
        self.marathon.clear_sites()
        for site in context.args:
            self.marathon.add_site(site)

        # TODO: replace joins with f-strings
        text = '\n'.join(("Successfully set sites to:", self._sites_text()))
        update.message.reply_markdown(text=text)

    @cmdhandler(pass_context=True)
    @marathon_method
    def add_participants(self, update: tg.Update, context: tge.CallbackContext):
        """Add participants to marathon"""

        def msg_lines(p: mth.Participant):
            yield "Added *{}* to marathon:".format(p.name)
            for site in self.marathon.sites:
                user = p.user(site)
                yield " - _{}_ : [user ID {}]({})".format(mth.SITES[site]['name'], user.id,
                                                          user.link)
            yield ""
            yield "Please verify the IDs are correct."

        for username in context.args:
            self.marathon.add_participant(username)
            update.message.reply_markdown(
                text='\n'.join(msg_lines(self.marathon.participants[username])),
                disable_web_page_preview=True)

    # TODO: remove participant

    @cmdhandler(pass_context=True)
    @marathon_method
    def set_duration(self, update: tg.Update, context: tge.CallbackContext):
        args = context.args
        try:
            hours, minutes = 0, 0
            if len(args) == 1:
                hours = int(args[0])
            elif len(args) == 2:
                hours, minutes = int(args[0]), int(args[1])
            else:
                raise ArgCountError("Expected one or two argument")

            self.marathon.duration = datetime.timedelta(hours=hours, minutes=minutes)
            update.message.reply_markdown(
                "Set the duration to *{}* (_hh:mm:ss_ )".format(self.marathon.duration))
        except ValueError:
            raise ArgValueError("Invalid duration given")

    @cmdhandler(pass_context=True)
    def schedule(self, update: tg.Update, context: tge.CallbackContext):
        args = context.args
        try:
            day, time_of_day = datetime.date.today(), datetime.time()
            if len(args) == 1:
                hour_num, minute_num = (int(num) for num in args[1].split(':'))
                time_of_day = datetime.time(hour=hour_num, minute=minute_num)
            elif len(args) == 2:
                day_num, month_num, year_num = (int(num) for num in args[0].split('/'))
                day = datetime.date(year=year_num, month=month_num, day=day_num)
                hour_num, minute_num = (int(num) for num in args[1].split(':'))
                time_of_day = datetime.time(hour=hour_num, minute=minute_num)
            else:
                raise ArgCountError("Expected one or two arguments")

            date_time = datetime.datetime.combine(day, time_of_day)
            JOB_QUEUE.run_once(callback=self.start_scheduled_marathon,
                               when=date_time, context=self.id)
            update.message.reply_markdown("Scheduled marathon start for *{}*".format(date_time))
        except ValueError:
            raise ArgValueError("Invalid date/time given")

    @cmdhandler()
    def start_marathon(self, update: tg.Update):
        text = '\n\n'.join(["Starting the marathon with the following settings:",
                            self._settings_text(),
                            "Continue?\t/yes\t /no"])
        self.operation = OngoingOperation.START_MARATHON
        update.message.reply_markdown(text=text)

    @cmdhandler()
    @marathon_method
    def status(self, update: tg.Update):
        update.message.reply_markdown(text=self._status_text())

    @cmdhandler()
    @marathon_method
    def leaderboard(self, update: tg.Update):
        update.message.reply_markdown(text=self._leaderboard_text())

    @cmdhandler()
    def pause_marathon(self, update: tg.Update):
        # TODO: implement pause_marathon
        raise NotImplementedError

    @cmdhandler()
    def stop_marathon(self, update: tg.Update):
        # TODO: implement stop_marathon
        raise NotImplementedError

    # ---------------------------------- Job callbacks  ----------------------------------

    @job_callback()
    def send_status_update(self):
        text = '\n\n'.join((self._status_text(), self._leaderboard_text()))
        BOT.send_message(chat_id=self.id, text=text, parse_mode=ParseMode.MARKDOWN)

    @job_callback()
    def countdown(self):
        _, remaining = self.marathon.elapsed_remaining()
        seconds = int(remaining.total_seconds())
        minutes = seconds//60
        if minutes >= 1:
            text = "*{} minutes remaining!*".format(minutes)
        else:
            text = "_*{} seconds remaining!*_".format(seconds)
        BOT.send_message(chat_id=self.id, text=text, parse_mode=ParseMode.MARKDOWN)

    @job_callback()
    def start_scheduled_marathon(self):
        self._start_marathon()

    # ---------------------------------- Utility methods  ----------------------------------

    def check_marathon_created(self) -> None:
        if not self.marathon:
            raise UsageError("Marathon not yet created", help_txt=MARATHON_NOT_CREATED_TXT)

    def check_marathon_running(self) -> None:
        if not self.marathon.is_running:
            raise UsageError("Only available while marathon is running")

    def check_operation_ongoing(self) -> None:
        if self.operation is None:
            raise UsageError("No ongoing operation")

    def _settings_text(self) -> str:
        def lines():
            yield "Current settings for marathon:"
            yield self._sites_text()
            yield self._participants_text()
            yield "*Duration*: {} (_hh:mm:ss_ )".format(self.marathon.duration)

        return '\n\n'.join(lines())

    def _sites_text(self) -> str:
        def lines():
            yield "*Sites*:"
            for site in self.marathon.sites:
                yield "\t - _{}_".format(mth.SITES[site]['name'])

        return '\n'.join(lines())

    def _participants_text(self) -> str:
        def lines():
            yield "*Sites*:"
            for site in self.marathon.sites:
                yield "\t - {}".format(mth.SITES[site]['name'])

        return '\n'.join(lines())

    def _leaderboard_text(self) -> str:
        def lines():
            yield "LEADERBOARD\n"
            participants = self.marathon.participants.values()
            for i, p in enumerate(sorted(participants, key=lambda x: x.score)):
                yield "{}. *{}* – {} points".format(i, p, p.score)

        return '\n'.join(lines())

    def _status_text(self) -> str:
        if self.marathon.is_running:
            elapsed, remaining = self.marathon.elapsed_remaining()
            with open('text/running_status.md') as text:
                return text.read().strip().format(elapsed, remaining)
        else:
            return "Marathon is not running"

    def _start_marathon(self):
        self.marathon.start(target=self._marathon_update_handler())
        BOT.send_message(chat_id=self.id, text="*_Alright, marathon has begun!_*",
                         parse_mode=ParseMode.MARKDOWN)
        JOB_QUEUE.run_repeating(name='periodic updates',
                                callback=self.send_status_update,
                                interval=self.marathon.refresh_interval,
                                context=self.id)
        JOB_QUEUE.run_repeating(name='minute countdown',
                                callback=self.countdown,
                                interval=datetime.timedelta(minutes=1),
                                first=self.marathon.end_time - datetime.timedelta(minutes=5),
                                context=self.id)
        JOB_QUEUE.run_repeating(name='15 seconds countdown',
                                callback=self.countdown,
                                interval=datetime.timedelta(seconds=45),
                                first=self.marathon.end_time - datetime.timedelta(seconds=45),
                                context=self.id)
        JOB_QUEUE.run_repeating(name='5 seconds countdown',
                                callback=self.countdown,
                                interval=datetime.timedelta(seconds=1),
                                first=self.marathon.end_time - datetime.timedelta(seconds=5),
                                context=self.id)

    @coroutine
    def _marathon_update_handler(self):
        while True:
            update: mth.Update = (yield)

            def per_site():
                for site, increment in update.per_site.items():
                    yield " _{}_  ({:+})".format(mth.SITES[site]['name'], increment)

            text = "*{}* just gained *{:+}* reputation on".format(update.participant, update.total)
            text += ', '.join(per_site())
            BOT.send_message(chat_id=self.id, text=text, parse_mode=ParseMode.MARKDOWN)

    def _shutdown(self):
        self.marathon.destroy()
        for job in JOB_QUEUE.jobs():
            job.schedule_removal()
        del BotSession.SESSIONS[self.id]
        BOT.send_message(chat_id=self.id, text="I'm now sleeping. Reactivate with /start.")


def start_bot():
    logging.info("Starting bot")
    JOB_QUEUE.run_repeating(callback=save_jobs_job, interval=datetime.timedelta(minutes=1))
    try:
        load_jobs(JOB_QUEUE)
    except FileNotFoundError:
        pass

    UPDATER.start_polling()


def shutdown_bot():
    logging.info("Shutting down bot")
    UPDATER.stop()
    for chat in BotSession.SESSIONS:
        BOT.send_message(chat_id=chat,
                         text="*SERVER SHUTDOWN* – Going to sleep with the fishes...",
                         parse_mode=ParseMode.MARKDOWN)
    save_jobs(JOB_QUEUE)


atexit.register(shutdown_bot)

if __name__ == '__main__':
    start_bot()
    UPDATER.idle()
    shutdown_bot()
