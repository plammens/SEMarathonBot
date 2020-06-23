import logging

# TODO: extract script for running bot
# TODO: fix jobqueue persistence

if __name__ == '__main__':
    # noinspection SpellCheckingInspection
    logging.basicConfig(format='%(asctime)s - %(name)s:%(levelname)s - %(message)s',
                        level=logging.INFO)
    logging.info("Initializing module")

import atexit
import datetime
import functools
from typing import Any, Dict, Optional

import telegram as tg
import telegram.ext as tge
from telegram.parsemode import ParseMode
from markdown_strings import esc_format

from semarathon import marathon as mth
from semarathon.persistence import *
from semarathon.utils import *


# Construct bot objects:
TOKEN = load_text('token')
UPDATER = tge.Updater(token=TOKEN, use_context=True)
BOT: tg.Bot = UPDATER.bot
DISPATCHER: tge.Dispatcher = UPDATER.dispatcher
JOB_QUEUE: tge.JobQueue = UPDATER.job_queue

# Load text files:
# TODO: load all texts beforehand
# TODO: memoize load_text
USAGE_ERROR_TXT = load_text('usage-error')
INTERNAL_ERROR_TXT = load_text('internal-error')
INFO_TXT = load_text('info')
START_TXT = load_text('start')
MARATHON_NOT_CREATED_TXT = load_text('marathon_not_created')

# --------------------------------------- Helpers  ---------------------------------------

# type aliases
CommandCallback = Callable[[tg.Update, tge.CallbackContext], None]
CommandCallbackMethod = Callable[['BotSession', tg.Update, tge.CallbackContext], None]
BotSessionRunnable = Callable[['BotSession'], Any]


class UsageError(Exception):
    help_txt: str

    def __init__(self, *args, help_txt: str = None):
        super(UsageError, self).__init__(*args)
        self.help_txt = help_txt or "See /info for usage info"


class ArgValueError(UsageError, ValueError):
    pass


class ArgCountError(UsageError):
    pass


def get_session(context: tge.CallbackContext):
    try:
        return context.chat_data['session']
    except KeyError:
        raise UsageError("Session not initialized",
                         help_txt="You must use /start before using other commands")


# --------------------------------------- Decorators  ---------------------------------------


def cmdhandler(command: str = None, *, method: bool = True, **handler_kwargs) -> Decorator:
    """
    Decorator factory for command handlers. The returned decorator adds
    the decorated function as a command handler for the command ``command``
    to the global DISPATCHER. If ``command`` is not specified it defaults to
    the decorated function's name.

    The callback is also decorated with an exception handler before
    constructing the command handler.

    :param command: name of bot command to add a handler for
    :param method: whether the callback is in the form of a BotSession method
    :param handler_kwargs: additional keyword arguments for the
                           creation of the command handler (these will be passed
                           to ``telegram.ext.dispatcher.add_handler``)
    :return: the decorated function, unchanged
    """

    # noinspection PyPep8Naming
    T = TypeVar('T', CommandCallback, CommandCallbackMethod)

    # Actual decorator
    def decorator(callback: T) -> T:
        command_ = command or callback.__name__

        @functools.wraps(callback)
        def decorated(update: tg.Update, context: tge.CallbackContext):
            command_info = f'/{command_}@{update.effective_chat.id}'
            logging.info(f'reached {command_info}')
            try:
                # Build arguments list:
                args = [update, context]
                if method: args.insert(0, get_session(context))

                # Actual call:
                # TODO: send typing action?
                callback(*args)

                logging.info(f'served {command_info}')
            except (UsageError, ValueError, mth.SEMarathonError) as e:
                text = f"{USAGE_ERROR_TXT}\n{format_exception_md(e)}\n\n" \
                       f"{esc_format(getattr(e, 'help_txt', 'See /info for usage info'))}"
                markdown_safe_reply(update.message, text)
                logging.info(f'served {command_info} (with usage/algorithm error)')
            except Exception as e:
                text = f"{INTERNAL_ERROR_TXT}"
                markdown_safe_reply(update.message, text)
                logging.error(f'{command_info}: unexpected exception', exc_info=e)
            finally:
                logging.debug(f'exiting {command_info}')

        handler = tge.CommandHandler(command_, decorated, **handler_kwargs)
        DISPATCHER.add_handler(handler)
        return callback

    return decorator


def job_callback(pass_session: bool = True, pass_bot: bool = False) -> Callable:
    """Returns specialized decorator for Job callback functions"""

    def decorator(callback: Callable) -> Callable:
        """Actual decorator"""

        @functools.wraps(callback)
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


def marathon_method(method: Callable) -> Callable:
    @functools.wraps(method)
    def decorated_method(session: 'BotSession', *args, **kwargs):
        assert session.marathon is not None
        session.check_marathon_created()
        method(session, *args, **kwargs)

    return decorated_method


def running_marathon_method(method: Callable) -> Callable:
    @functools.wraps(method)
    def decorated_method(session: 'BotSession', *args, **kwargs):
        session.check_marathon_created()
        session.check_marathon_running()
        method(session, *args, **kwargs)

    decorated_method.__name__ = method.__name__
    return decorated_method


def ongoing_operation_method(method: Callable) -> Callable:
    @functools.wraps(method)
    def decorated_method(session: 'BotSession', *args, **kwargs):
        session.check_operation_ongoing()
        method(session, *args, **kwargs)
        session.operation = None

    decorated_method.__name__ = method.__name__
    return decorated_method


def require_confirmation(op_name: str = None, *, target: BotSessionRunnable) -> Decorator:
    """For commands that define operations that require confirmation from the user"""

    def decorator(method: CommandCallbackMethod) -> Callable:
        op_name_ = op_name or method.__name__

        @functools.wraps(method)
        def decorated_method(session: 'BotSession', update: tg.Update,
                             context: tge.CallbackContext):
            session.operation = BotSession.Operation(op_name_, session, target)
            rvalue = method(session, update, context)
            session.send_message(f"Continue `{op_name_}`? \t/yes \t/no")
            return rvalue

        return decorated_method

    return decorator


# --------------------------------------- BotSession  ---------------------------------------

# noinspection PyUnusedLocal
class BotSession:
    class Operation:
        session: 'BotSession'
        target: BotSessionRunnable  # TODO: fix (should be BotSession method)

        def __init__(self, name: str, session: 'BotSession', target: BotSessionRunnable):
            self.session = session
            self.target = target
            self.name = name

        def execute(self):
            self.target(self.session)

        def cancel(self):
            self.session.operation = None


    id: int
    marathon: Optional[mth.Marathon]
    operation: Optional[Operation]

    SESSIONS: Dict[int, 'BotSession'] = {}

    def __init__(self, chat_id: int):
        BotSession.SESSIONS[chat_id] = self
        self.id = chat_id
        self.marathon = None
        self.operation = None

    # ---------------------------------- Command handlers  ----------------------------------

    @staticmethod
    @cmdhandler(method=False)
    def info(update: tg.Update, context: tge.CallbackContext):
        """Show info message"""
        update.message.reply_markdown(INFO_TXT)

    @staticmethod
    @cmdhandler(method=False)
    def start(update: tg.Update, context: tge.CallbackContext):
        """Start session"""
        context.chat_data['session'] = BotSession(update.message.chat_id)
        update.message.reply_text(text=START_TXT)

    def _shutdown(self):
        self.marathon.destroy()
        for job in JOB_QUEUE.jobs():
            job.schedule_removal()
        del BotSession.SESSIONS[self.id]
        self.send_message(text="I'm now sleeping. Reactivate with /start.", parse_mode=None)

    @cmdhandler()
    @require_confirmation(target=_shutdown)
    def shutdown(self, update: tg.Update, context: tge.CallbackContext):
        update.message.reply_text("Shutting down...")

    @cmdhandler()
    @ongoing_operation_method
    def yes(self, update: tg.Update, context: tge.CallbackContext):
        self.operation.execute()

    @cmdhandler()
    @ongoing_operation_method
    def no(self, update: tg.Update, context: tge.CallbackContext):
        self.cancel(update)

    @cmdhandler()
    @ongoing_operation_method
    def cancel(self, update: tg.Update, context: tge.CallbackContext):
        self.send_message(f"Operation cancelled: `{self.operation.name}`")

    @cmdhandler()
    def new_marathon(self, update: tg.Update, context: tge.CallbackContext):
        """Create new marathon"""
        self.marathon = mth.Marathon()
        with open('text/new_marathon.txt') as text:
            update.message.reply_markdown(text=text.read().strip())

    @cmdhandler()
    @marathon_method
    def settings(self, update: tg.Update, context: tge.CallbackContext):
        """Show settings"""
        text = f"Current settings for marathon:\n\n{self._settings_text()}"
        update.message.reply_markdown(text=text)

    @cmdhandler()
    @marathon_method
    def set_sites(self, update: tg.Update, context: tge.CallbackContext):
        self.marathon.clear_sites()
        for site in context.args:
            self.marathon.add_site(site)

        text = f"Successfully set sites to:\n{self._sites_text()}"
        update.message.reply_markdown(text=text)

    @cmdhandler()
    @marathon_method
    def add_participants(self, update: tg.Update, context: tge.CallbackContext):
        """Add participants to marathon"""

        def msg_lines(p: mth.Participant):
            yield f"Added *{p.name}* to marathon:"
            for site in self.marathon.sites:
                user = p.user(site)
                yield f" - _{mth.SITES[site]['name']}_ : [user ID {user.id}]({user.link})"
            yield ""
            yield "Please verify the IDs are correct."

        for username in context.args:
            self.marathon.add_participant(username)
            update.message.reply_markdown(
                text='\n'.join(msg_lines(self.marathon.participants[username])),
                disable_web_page_preview=True)

    # TODO: remove participant

    @cmdhandler()
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
                f"Set the duration to *{self.marathon.duration}* (_hh:mm:ss_ )")
        except ValueError:
            raise ArgValueError("Invalid duration given")

    @cmdhandler()
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
            update.message.reply_markdown(f"Scheduled marathon start for *{date_time}*")
        except ValueError:
            raise ArgValueError("Invalid date/time given")

    def _start_marathon(self):
        self.marathon.start(target=self._marathon_update_handler())
        self.send_message("*_Alright, marathon has begun!_*")
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

    @cmdhandler()
    @require_confirmation(target=_start_marathon)
    def start_marathon(self, update: tg.Update, context: tge.CallbackContext):
        text = f"Starting the marathon with the following settings:\n\n{self._settings_text()}"
        update.message.reply_markdown(text=text)

    @cmdhandler()
    @marathon_method
    def status(self, update: tg.Update, context: tge.CallbackContext):
        update.message.reply_markdown(text=self._status_text())

    @cmdhandler()
    @marathon_method
    def leaderboard(self, update: tg.Update, context: tge.CallbackContext):
        update.message.reply_markdown(text=self._leaderboard_text())

    @cmdhandler()
    @marathon_method
    def time(self, update: tg.Update, context: tge.CallbackContext):
        # TODO: implement time
        raise NotImplementedError

    @cmdhandler()
    def pause_marathon(self, update: tg.Update, context: tge.CallbackContext):
        # TODO: implement pause_marathon
        raise NotImplementedError

    # ---------------------------------- Job callbacks  ----------------------------------

    @cmdhandler()
    def stop_marathon(self, update: tg.Update, context: tge.CallbackContext):
        # TODO: implement stop_marathon
        raise NotImplementedError

    @job_callback()
    def send_status_update(self):
        text = f"{self._status_text()}\n\n{self._leaderboard_text()}"
        self.send_message(text)

    @job_callback()
    def countdown(self):
        _, remaining = self.marathon.elapsed_remaining
        seconds = int(remaining.total_seconds())
        minutes = seconds//60
        fmt = f"{minutes} minutes" if minutes >= 1 else f"{seconds} seconds"
        self.send_message(f"*{fmt} remaining!*")

    # ---------------------------------- Utility methods  ----------------------------------

    @job_callback()
    def start_scheduled_marathon(self):
        self._start_marathon()

    def check_marathon_created(self) -> None:
        if not self.marathon:
            raise UsageError("Marathon not yet created", help_txt=MARATHON_NOT_CREATED_TXT)

    def check_marathon_running(self) -> None:
        if not self.marathon.is_running:
            raise UsageError("Only available while marathon is running")

    def check_operation_ongoing(self) -> None:
        if self.operation is None:
            raise UsageError("No ongoing operation")

    def send_message(self, text, parse_mode=ParseMode.MARKDOWN):
        BOT.send_message(chat_id=self.id, text=text, parse_mode=parse_mode)

    def _settings_text(self) -> str:
        def lines():
            yield self._sites_text()
            yield self._participants_text()
            yield f"*Duration*: {self.marathon.duration} (_hh:mm:ss_ )"

        return '\n\n'.join(lines())

    def _sites_text(self) -> str:
        def lines():
            yield "*Sites*:"
            for site in self.marathon.sites:
                yield f"\t - _{mth.SITES[site]['name']}_"

        return '\n'.join(lines())

    def _participants_text(self) -> str:
        def lines():
            yield "*Sites*:"
            for site in self.marathon.sites:
                yield f"\t - {mth.SITES[site]['name']}"

        return '\n'.join(lines())

    def _leaderboard_text(self) -> str:
        def lines():
            yield "LEADERBOARD\n"
            participants = self.marathon.participants.values()
            for i, p in enumerate(sorted(participants, key=lambda x: x.score)):
                yield f"{i}. *{p}* – {p.score} points"

        return '\n'.join(lines())

    def _status_text(self) -> str:
        if self.marathon.is_running:
            elapsed, remaining = self.marathon.elapsed_remaining
            with open('text/running_status.md') as text:
                return text.read().strip().format(elapsed, remaining)
        else:
            return "Marathon is not running"

    @coroutine
    def _marathon_update_handler(self):
        while True:
            update: mth.Update = (yield)

            def per_site():
                for site, increment in update.per_site.items():
                    yield f" _{mth.SITES[site]['name']}_  ({increment:+})"

            text = f"*{update.participant}* just gained *{update.total:+}* reputation on"
            text += ', '.join(per_site())
            self.send_message(text)


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
    for chat in BotSession.SESSIONS.copy():
        BOT.send_message(chat_id=chat,
                         text="*SERVER SHUTDOWN* – Going to sleep with the fishes...",
                         parse_mode=ParseMode.MARKDOWN)
        del BotSession.SESSIONS[chat]
    save_jobs(JOB_QUEUE)


atexit.register(shutdown_bot)

if __name__ == '__main__':
    start_bot()
    UPDATER.idle()
    shutdown_bot()
