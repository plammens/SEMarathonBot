import datetime
import enum
import functools
import inspect
import itertools
import logging
import time
from typing import Any, Callable, Generator, List, Optional, TypeVar

import more_itertools
import telegram as tg
import telegram.ext as tge
from telegram.parsemode import ParseMode
from telegram.utils.helpers import escape_markdown as escape_md

from semarathon import marathon as mth
from semarathon.utils import Decorator, Text, coroutine, format_exception_md

# logger setup
logger = logging.getLogger(__name__)
del logging  # to avoid mistakes with code completion

# type aliases
CommandCallback = Callable[[tg.Update, tge.CallbackContext], None]
CommandCallbackMethod = Callable[["BotSession", tg.Update, tge.CallbackContext], None]
BotSessionRunnable = Callable[["BotSession"], Any]
T = TypeVar("T", CommandCallback, CommandCallbackMethod)

# other aliases
escape_mdv2 = functools.partial(escape_md, version=2)


# ------------------------------- Decorators  -------------------------------


class _CommandCallbackType(enum.Enum):
    FREE_FUNCTION = enum.auto()
    BOT_SYSTEM_METHOD = enum.auto()
    SESSION_METHOD = enum.auto()


def _make_command_handler(
    callback: T,
    command: str = None,
    *,
    callback_type: _CommandCallbackType = _CommandCallbackType.SESSION_METHOD,
    **handler_kwargs,
) -> tge.CommandHandler:
    """Make a command handler for the command ``command``

    Constructs a :class:`telegram.ext.CommandHandler` with a decorated version of
    the given callback. If ``command`` is not specified it defaults to the callback
    function's name. The callback is decorated with an exception handler and some
    logging business.

    :param callback: original callback for the command
    :param command: see :func:`cmdhandler`
    :param callback_type: see :func:`cmdhandler`
    :param handler_kwargs: see :func:`cmdhandler`

    :return: a command handler for the given command
    """

    command = command or callback.__name__
    callback_type = _CommandCallbackType(callback_type)

    @functools.wraps(callback)
    def decorated(update: tg.Update, context: tge.CallbackContext):
        command_info = f"/{command}@{update.effective_chat.id}"
        logger.info(f"reached {command_info}")
        try:
            bot_system = _get_bot_system(context)

            # Build arguments list:
            args = [update, context]
            if callback_type == _CommandCallbackType.SESSION_METHOD:
                args.insert(0, _get_session(context))
            elif callback_type == _CommandCallbackType.BOT_SYSTEM_METHOD:
                args.insert(0, bot_system)

            # Actual call:
            callback(*args)

            logger.info(f"served {command_info}")
        except (UsageError, ValueError, mth.SEMarathonError) as e:
            text = (
                f"{Text.load('usage-error')}\n{format_exception_md(e)}\n\n"
                f"{escape_mdv2(getattr(e, 'help_txt', 'See /info for usage info'))}"
            )
            markdown_safe_send(context.bot, update.effective_chat.id, text)
            logger.info(f"served {command_info} (with usage/algorithm error)")
        except Exception as e:
            text = f"{Text.load('internal-error')}"
            markdown_safe_send(context.bot, update.effective_chat.id, text)
            logger.exception(f"{command_info}: unexpected exception", exc_info=e)
        finally:
            logger.debug(f"exiting {command_info}")

    handler = tge.CommandHandler(command, decorated, **handler_kwargs)
    return handler


def _extract_command_description(callback: Callable) -> str:
    doc = getattr(callback, "__doc__", None)
    if not doc:
        raise ValueError(f"No command description found for callback {callback}")
    return doc.strip().split("\n", maxsplit=1)[0]


def cmdhandler(
    command: str = None,
    *,
    callback_type: _CommandCallbackType = _CommandCallbackType.SESSION_METHOD,
    register: bool = True,
    **handler_kwargs,
) -> Decorator:
    """Parametrised decorator that marks a function as a callback for a command handler.

    :param command: name of bot command to add a handler for
    :param callback_type: type of callback ("standard" top-level function, bot system
                          method, or session method)
    :param register: whether to register the command in the command list shown on
                     Telegram clients. If set to ``True``, a ``command_info`` attribute
                     is added to the callback function containing a BotCommand object,
                     and a positive integer is assigned to ``callback.register``, in
                     order of usage of this decorator.
                     Otherwise ``callback.register`` is set to ```False``.
    :param handler_kwargs: additional keyword arguments for the
                           creation of the command handler (these will be passed
                           to ``telegram.ext.dispatcher.add_handler``)

    :return: (after decoration) the decorated function, with the added
        ``command_handler`` and (optionally) ``command_info`` attributes.
    """

    def decorator(callback: T) -> T:
        command_ = command or callback.__name__

        handler = _make_command_handler(
            callback,
            command_,
            callback_type=callback_type,
            **handler_kwargs,
        )
        callback.command_handler = handler
        if register:
            callback.register = cmdhandler._counter = (
                getattr(cmdhandler, "_counter", 0) + 1
            )
            callback.command_info = tg.BotCommand(
                command_, _extract_command_description(callback)
            )
        else:
            callback.register = False

        return callback

    return decorator


def marathon_method(method: Callable) -> Callable:
    @functools.wraps(method)
    def decorated_method(session: "SEMarathonBotSystem.Session", *args, **kwargs):
        session.check_marathon_created()
        method(session, *args, **kwargs)

    return decorated_method


def running_marathon_method(method: Callable) -> Callable:
    @functools.wraps(method)
    def decorated_method(session: "SEMarathonBotSystem.Session", *args, **kwargs):
        session.check_marathon_created()
        session.check_marathon_running()
        method(session, *args, **kwargs)

    return decorated_method


def ongoing_operation_method(method: Callable) -> Callable:
    @functools.wraps(method)
    def decorated_method(session: "SEMarathonBotSystem.Session", *args, **kwargs):
        session.check_operation_ongoing()
        method(session, *args, **kwargs)
        session.operation = None

    return decorated_method


def require_confirmation(
    op_name: str = None, *, target: BotSessionRunnable
) -> Decorator:
    """For commands that define operations that require confirmation from the user"""

    def decorator(method: CommandCallbackMethod) -> Callable:
        op_name_ = op_name or method.__name__

        @functools.wraps(method)
        def decorated_method(
            session: "SEMarathonBotSystem.Session",
            update: tg.Update,
            context: tge.CallbackContext,
        ):
            session.operation = SEMarathonBotSystem.Session.Operation(
                op_name_, session, target
            )
            rvalue = method(session, update, context)
            session.send_message(f"Continue `{op_name_}`? \t/yes \t/no")
            return rvalue

        return decorated_method

    return decorator


# ------------------------------- BotSession  -------------------------------

# TODO: split command handlers into core functionality and side effect


class SEMarathonBotSystem:
    """
    Manages all the components of one instance of the SE Marathon Bot.

    Each instance of the SE Marathon Bot corresponds one-to-one with a Telegram bot
    username. This class allows deployment of the same abstract bot behaviour to any
    Telegram bot (i.e. bot username).
    """

    bot: tg.Bot
    updater: tge.Updater
    dispatcher: tge.Dispatcher
    job_queue: tge.JobQueue

    def __init__(self, token: str, **kwargs):
        """
        Initialize a new bot instance and bind it to a certain bot username.

        Arguments are the same as for :class:`telegram.ext.Updater` with the exception
        of use_context, which is automatically set to ``True`` (and cannot be changed).
        """

        self.updater = tge.Updater(token, use_context=True, **kwargs)
        self.bot = self.updater.bot
        self.dispatcher = self.updater.dispatcher
        self.job_queue = self.updater.job_queue

        self._setup_handlers()
        self.dispatcher.bot_data["bot_system"] = self

    @property
    def sessions(self):
        return {
            chat_id: chat_data["session"]
            for chat_id, chat_data in self.dispatcher.chat_data.items()
            if "session" in chat_data
        }

    # noinspection PyUnusedLocal
    @staticmethod
    @cmdhandler(callback_type=_CommandCallbackType.FREE_FUNCTION)
    def info(update: tg.Update, context: tge.CallbackContext):
        """General information about this bot and credits"""
        update.message.reply_markdown_v2(Text.load("info"))

    # noinspection PyUnusedLocal
    @cmdhandler(callback_type=_CommandCallbackType.BOT_SYSTEM_METHOD)
    def start(self, update: tg.Update, context: tge.CallbackContext) -> "Session":
        """Start a session in the current chat (start listening for commands)"""
        chat_id = update.message.chat_id
        session = SEMarathonBotSystem.Session(self, chat_id)
        update.message.reply_text(text=Text.load("start"))
        return session

    # noinspection PyUnusedLocal
    class Session:
        """Represents the context of the interaction with the bot in a Telegram chat."""

        bot_system: "SEMarathonBotSystem"
        id: int
        marathon: Optional[mth.Marathon]
        operation: Optional["SEMarathonBotSystem.Session.Operation"]
        jobs: List[tge.Job]

        def __init__(self, bot_system: "SEMarathonBotSystem", chat_id: int):
            """Initialize a new session and attach it to the given chat."""
            self.bot_system = bot_system
            self.id = chat_id
            self.marathon = None
            self.operation = None
            self.jobs = []

            self.bot_system.dispatcher.chat_data[chat_id]["session"] = self

        class Operation:
            session: "SEMarathonBotSystem.Session"
            target: BotSessionRunnable  # TODO: fix (should be BotSession method)

            def __init__(
                self,
                name: str,
                session: "SEMarathonBotSystem.Session",
                target: BotSessionRunnable,
            ):
                self.session = session
                self.target = target
                self.name = name

            def execute(self):
                self.target(self.session)

            def cancel(self):
                self.session.operation = None

        # -------------------------- Command handlers  --------------------------

        @cmdhandler()
        def new_marathon(
            self, update: tg.Update, context: tge.CallbackContext
        ) -> mth.Marathon:
            """Create a new marathon"""
            self.marathon = mth.Marathon()
            self.send_message(text=Text.load("new-marathon"))
            return self.marathon

        @cmdhandler()
        @marathon_method
        def settings(self, update: tg.Update, context: tge.CallbackContext):
            """View current settings for the marathon"""
            text = f"Current settings for marathon:\n\n{self._settings_text()}"
            self.send_message(text=text)

        @cmdhandler()
        @marathon_method
        def set_sites(self, update: tg.Update, context: tge.CallbackContext):
            """Set the SE sites to be tracked during the marathon"""
            self.marathon.clear_sites()
            for site in context.args:
                self.marathon.add_site(site)

            text = f"Successfully set sites to:\n{self._sites_text()}"
            self.send_message(text=text)

        @cmdhandler()
        @marathon_method
        def add_participants(self, update: tg.Update, context: tge.CallbackContext):
            """Add participants to the marathon"""

            def lines(p: mth.Participant):
                yield f"Added *{p.name}* to marathon:"
                for site in self.marathon.sites:
                    user = p.user_profiles[site]
                    yield (
                        rf" \- _{escape_mdv2(mth.SITES[site]['name'])}_ : "
                        f"[user ID {user.user_id}]({user.link})"
                    )
                yield ""
                yield r"Please verify the IDs are correct\."

            for name, network_id in more_itertools.pairwise(context.args):
                participant = mth.Participant(self.marathon, name, int(network_id))
                self.marathon.add_participant(participant)
                text = "\n".join(lines(participant))
                self.send_message(text=text, disable_web_page_preview=True)

        # TODO: remove participant

        @cmdhandler()
        @marathon_method
        def set_duration(self, update: tg.Update, context: tge.CallbackContext):
            """Set the duration for the marathon"""
            args = context.args
            try:
                hours, minutes = 0, 0
                if len(args) == 1:
                    hours = int(args[0])
                elif len(args) == 2:
                    hours, minutes = int(args[0]), int(args[1])
                else:
                    raise ArgCountError("Expected one or two argument")

                self.marathon.duration = duration = datetime.timedelta(
                    hours=hours, minutes=minutes
                )
                self.send_message(
                    rf"Set the duration to "
                    rf"*{escape_mdv2(str(duration))}* \(_hh:mm:ss_\)"
                )
            except ValueError:
                raise ArgValueError("Invalid duration given")

        @cmdhandler()
        @marathon_method
        def schedule(self, update: tg.Update, context: tge.CallbackContext):
            """Schedule the start of the marathon"""
            args = context.args
            try:
                day, time_of_day = datetime.date.today(), datetime.time()
                if len(args) == 1:
                    hour_num, minute_num = (int(num) for num in args[1].split(":"))
                    time_of_day = datetime.time(hour=hour_num, minute=minute_num)
                elif len(args) == 2:
                    day_num, month_num, year_num = (
                        int(num) for num in args[0].split("/")
                    )
                    day = datetime.date(year=year_num, month=month_num, day=day_num)
                    hour_num, minute_num = (int(num) for num in args[1].split(":"))
                    time_of_day = datetime.time(hour=hour_num, minute=minute_num)
                else:
                    raise ArgCountError("Expected one or two arguments")

                date_time = datetime.datetime.combine(day, time_of_day)
                self.bot_system.job_queue.run_once(
                    callback=self.start_scheduled_marathon,
                    when=date_time,
                    context=self.id,
                )
                self.send_message(
                    f"Scheduled marathon start for *{escape_mdv2(str(date_time))}*"
                )
            except ValueError:
                raise ArgValueError("Invalid date/time given")

        def _start_marathon(self):
            self.marathon.start(handler=self._marathon_update_handler())
            self.send_message(r"*_Alright, marathon has begun\!_*")
            self.jobs.append(
                self.bot_system.job_queue.run_repeating(
                    name="periodic updates",
                    callback=self.send_status_update,
                    interval=self.marathon.duration / 10,
                    context=self.id,
                )
            )
            self.jobs.append(
                self.bot_system.job_queue.run_repeating(
                    name="minute countdown",
                    callback=self.countdown,
                    interval=datetime.timedelta(minutes=1),
                    first=self.marathon.end_time - datetime.timedelta(minutes=5),
                    context=self.id,
                )
            )
            self.jobs.append(
                self.bot_system.job_queue.run_repeating(
                    name="15 seconds countdown",
                    callback=self.countdown,
                    interval=datetime.timedelta(seconds=45),
                    first=self.marathon.end_time - datetime.timedelta(seconds=45),
                    context=self.id,
                )
            )
            self.jobs.append(
                self.bot_system.job_queue.run_repeating(
                    name="5 seconds countdown",
                    callback=self.countdown,
                    interval=datetime.timedelta(seconds=1),
                    first=self.marathon.end_time - datetime.timedelta(seconds=5),
                    context=self.id,
                )
            )

        @cmdhandler()
        @require_confirmation(target=_start_marathon)
        def start_marathon(self, update: tg.Update, context: tge.CallbackContext):
            """Start the marathon"""
            text = (
                f"Starting the marathon with the following settings:\n\n"
                f"{self._settings_text()}"
            )
            self.send_message(text=text)

        @cmdhandler()
        @marathon_method
        def status(self, update: tg.Update, context: tge.CallbackContext):
            """Show the status of the current marathon"""
            self.send_message(text=self._status_text())

        @cmdhandler()
        @marathon_method
        def leaderboard(self, update: tg.Update, context: tge.CallbackContext):
            """Show the leaderboard"""
            self.send_message(text=self._leaderboard_text())

        @cmdhandler()
        @running_marathon_method
        def time(
            self, update: tg.Update, context: tge.CallbackContext
        ) -> datetime.timedelta:
            """Time remaining until the end of the marathon"""
            remaining = self.marathon.end_time - datetime.datetime.now()
            self.send_message(f"*Time remaining:* {escape_mdv2(str(remaining))}")
            return remaining

        @cmdhandler()
        @running_marathon_method
        def pause_marathon(self, update: tg.Update, context: tge.CallbackContext):
            """Pause the marathon while it is running"""
            # TODO: implement pause_marathon
            raise NotImplementedError

        @cmdhandler()
        def stop_marathon(self, update: tg.Update, context: tge.CallbackContext):
            """Stop the marathon prematurely"""
            self.marathon.stop()
            assert not self.marathon.is_running

        def _shutdown(self, message=True):
            if self.marathon is not None:
                self.marathon.stop()
            for job in self.bot_system.job_queue.jobs():
                job.schedule_removal()
            del self.bot_system.dispatcher.chat_data[self.id]["session"]
            if message:
                self.send_message(
                    text="I'm now sleeping. Reactivate with /start.", parse_mode=None
                )

        @cmdhandler()
        @require_confirmation(target=_shutdown)
        def shutdown(self, update: tg.Update, context: tge.CallbackContext):
            """End the current session"""
            self.send_message("Shutting down...", parse_mode=None)

        # ----- Ongoing operation command callbacks  -----

        @cmdhandler(register=False)
        @ongoing_operation_method
        def yes(self, update: tg.Update, context: tge.CallbackContext):
            """Confirm an active operation"""
            self.operation.execute()

        @cmdhandler(register=False)
        @ongoing_operation_method
        def no(self, update: tg.Update, context: tge.CallbackContext):
            """Cancel an active operation"""
            self.cancel(update, context)

        @cmdhandler(register=False)
        @ongoing_operation_method
        def cancel(self, update: tg.Update, context: tge.CallbackContext):
            """Cancel an active operation"""
            self.send_message(f"Operation cancelled: `{self.operation.name}`")

        # ------------------------------- Job callbacks  ----------------------------

        def send_status_update(self, context: tge.CallbackContext):
            text = f"{self._status_text()}\n\n{self._leaderboard_text()}"
            self.send_message(text)

        def countdown(self, context: tge.CallbackContext):
            _, remaining = self.marathon.elapsed_remaining
            seconds = int(remaining.total_seconds())
            minutes = seconds // 60
            fmt = f"{minutes} minutes" if minutes >= 1 else f"{seconds} seconds"
            self.send_message(f"*{fmt} remaining!*")

        def start_scheduled_marathon(self, context: tge.CallbackContext):
            self._start_marathon()

        # ---------------------------- Utility methods  ----------------------------

        def check_marathon_created(self) -> None:
            if not self.marathon:
                raise UsageError(
                    "Marathon not yet created",
                    help_txt=Text.load("marathon-not-created"),
                )

        def check_marathon_running(self) -> None:
            if not self.marathon.is_running:
                raise UsageError("Only available while marathon is running")

        def check_operation_ongoing(self) -> None:
            if self.operation is None:
                raise UsageError("No ongoing operation")

        def send_message(self, text, parse_mode=ParseMode.MARKDOWN_V2, **kwargs):
            """Send a message to the chat to which this session is associated

            :param text: text to send in the message
            :param parse_mode: parse mode to use. If the `text` parameter has a
                               ``parse_mode`` attribute, that is used instead and this
                               parameter is ignored.
            """
            if hasattr(text, "parse_mode"):
                parse_mode = text.parse_mode
            return markdown_safe_send(self.bot_system.bot, self.id, text, parse_mode)

        def _settings_text(self) -> str:
            def lines():
                yield self._sites_text()
                yield self._participants_text()
                yield rf"*Duration*: {self.marathon.duration} \(_hh:mm:ss_\)"

            return "\n\n".join(lines())

        def _sites_text(self) -> str:
            def lines():
                yield "*Sites*:"
                for site in self.marathon.sites:
                    site_name_md = escape_mdv2(mth.SITES[site]["name"])
                    yield f"\t\\- _{site_name_md}_"

            return "\n".join(lines())

        def _participants_text(self) -> str:
            def lines():
                yield "*Participants*:"
                for name, participant in self.marathon.participants.items():
                    yield rf" \- {name} \({participant.network_id}\)"

            return "\n".join(lines())

        def _leaderboard_text(self) -> str:
            def lines():
                yield "__LEADERBOARD__"
                participants = self.marathon.participants.values()
                leaderboard = sorted(participants, key=lambda x: x.score, reverse=True)
                for i, p in enumerate(leaderboard, 1):
                    yield rf"{i}\. *{escape_mdv2(str(p))}* – {p.score} points"

            return "\n".join(lines())

        def _status_text(self) -> str:
            if self.marathon.is_running:
                elapsed, remaining = self.marathon.elapsed_remaining
                return Text.load("running-status").format(
                    elapsed=escape_mdv2(str(elapsed)),
                    remaining=escape_mdv2(str(remaining)),
                )
            else:
                return "Marathon is not running"

        @coroutine
        def _marathon_update_handler(self) -> Generator[None, mth.ScoreUpdate, None]:
            try:
                while True:
                    update = yield
                    logger.debug(f"Received a marathon update for {update.participant}")
                    per_site = ", ".join(
                        f" _{mth.SITES[site]['name']}_  ({increment:+})"
                        for site, increment in update.per_site.items()
                    )
                    text = (
                        f"*{escape_mdv2(str(update.participant))}* just gained "
                        f"*{update.total:+}* reputation on {per_site}"
                    )
                    self.send_message(text)
            except GeneratorExit:
                # marathon has stopped (either at the scheduled time or prematurely)
                logger.debug("Notifying chat of end of marathon")
                self._marathon_end_handler()

        def _marathon_end_handler(self):
            for job in self.jobs:
                job.schedule_removal()
            self.send_message(r"_*Marathon has ended\!*_")
            scores = {p: p.score for p in self.marathon.participants.values()}
            winner, _ = max(scores.items(), key=lambda t: t[1], default=(None, None))
            self._send_winner(winner)
            self.send_message(self._leaderboard_text())

        def _send_winner(self, winner):
            lines = (f"And the winner is\\.\\.\\.", f"🎉🎉 *{winner}* 🎉🎉")
            message = self.send_message(lines[0])
            time.sleep(1)
            message.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)

    @classmethod
    def _collect_command_callbacks(cls):
        return [
            callback
            for name, callback in itertools.chain.from_iterable(
                inspect.getmembers(class_, inspect.isfunction)
                for class_ in (cls, cls.Session)
            )
            if hasattr(callback, "command_handler")
        ]

    def _setup_handlers(self):
        callbacks = self._collect_command_callbacks()
        for callback in callbacks:
            logger.debug(
                f"Adding command handler for {callback.command_handler.command}"
            )
            self.dispatcher.add_handler(callback.command_handler)

        cmd_list = [
            callback.command_info
            for callback in sorted(callbacks, key=lambda c: c.register)
            if callback.register
        ]
        logger.debug(
            f"Registering command list on Telegram: {[cmd.command for cmd in cmd_list]}"
        )
        self.bot.set_my_commands(cmd_list)


# Hide decorators (as they're intended to be used only with the above class)
del cmdhandler
del marathon_method
del running_marathon_method
del ongoing_operation_method
del require_confirmation


# ------------------------------- Exceptions  -------------------------------


class UsageError(Exception):
    help_txt: str

    def __init__(self, *args, help_txt: str = None):
        super(UsageError, self).__init__(*args)
        self.help_txt = help_txt or "See /info for usage info"


class ArgValueError(UsageError, ValueError):
    pass


class ArgCountError(UsageError):
    pass


# ------------------------------- Misc helpers  -------------------------------


def _get_bot_system(context: tge.CallbackContext) -> SEMarathonBotSystem:
    try:
        return context.bot_data["bot_system"]
    except KeyError:
        raise RuntimeError("Received an update destined for an uninitialised bot")


def _get_session(context: tge.CallbackContext) -> SEMarathonBotSystem.Session:
    try:
        return context.chat_data["session"]
    except KeyError:
        raise UsageError(
            "Session not initialized",
            help_txt="You must use /start before using other commands",
        )


def markdown_safe_send(
    bot: tg.Bot,
    chat_id: int,
    message: str,
    parse_mode: tg.ParseMode = ParseMode.MARKDOWN_V2,
) -> tg.Message:
    """
    Tries to reply to ``original_message`` in Markdown; falls back to plain text
    if it can't be parsed correctly.
    """
    all_modes = [ParseMode.MARKDOWN_V2, ParseMode.MARKDOWN]
    modes = all_modes[all_modes.index(parse_mode) :] if parse_mode else []
    for mode in modes:
        try:
            return bot.send_message(chat_id, message, parse_mode=mode)
        except tg.error.BadRequest as exc:
            logger.exception(f"Failed to parse as {mode}", exc_info=exc)
            continue
    return bot.send_message(chat_id, message, parse_mode=None)
