print("Initializing server... ", end='')

import atexit
import datetime
from enum import Enum
from typing import List, Dict

import telegram as tg
import telegram.ext as tge
import telegram.ext.filters as tgf
from telegram.parsemode import ParseMode

import marathon as sem
from jq_pickle import *
from utils import *


with open('token.txt') as token_file:
    TOKEN: str = token_file.read().strip()

UPDATER = tge.Updater(token=TOKEN)
BOT, DISPATCHER, JOB_QUEUE = UPDATER.bot, UPDATER.dispatcher, UPDATER.job_queue

"""Helper classes"""


class BotArgumentError(ValueError):
    pass


class OngoingOperation(Enum):
    START_MARATHON = "start marathon"
    SHUTDOWN = "shutdown"


"""Method decorators"""


def cmd_handler(cmd: str = None, *,
                pass_session: bool = True, pass_bot: bool = False, pass_update: bool = True,
                **cmd_handler_kwargs) -> callable:
    """Returns specialized decorator for CommandHandler callback functions"""

    def decorator(callback: callable) -> tge.CommandHandler:
        """Actual decorator"""
        nonlocal cmd
        if cmd is None: cmd = callback.__name__

        def decorated(bot, update, *args, **kwargs):
            chat_id = update.message.chat_id
            session = BotSession.sessions.get(chat_id, None)
            if pass_session and session is None: return

            effective_args = []
            if pass_session: effective_args.append(session)
            if pass_bot: effective_args.append(bot)
            if pass_update: effective_args.append(update)
            effective_args.extend(args)

            debug_print("/{} served".format(callback.__name__))
            return callback(*effective_args, **kwargs)

        handler = tge.CommandHandler(cmd, decorated, **cmd_handler_kwargs)
        DISPATCHER.add_handler(handler)
        return callback

    return decorator


def job_callback(pass_session: bool = True, pass_bot: bool = False) -> callable:
    """Returns specialized decorator for Job callback functions"""

    def decorator(callback: callable) -> callable:
        """Actual decorator"""

        def decorated(bot, job, *args, **kwargs):
            chat_id = job.context
            session = BotSession.sessions.get(chat_id, None)
            if pass_session and session is None: return

            effective_args = []
            if pass_session: effective_args.append(session)
            if pass_bot: effective_args.append(bot)
            effective_args.extend(args)

            return callback(*effective_args, **kwargs)

        return decorated

    return decorator


# def check(callback: callable, callback_args: tuple = None, callback_kwargs: dict = None):
#     def decorator(method: callable) -> callable:
#         def decorated(*args, **kwargs):
#             if not callback(*callback_args, **callback_kwargs): return
#             method(*args, **kwargs)
#
#         decorated.__name__ = method.__name__
#         return decorated
#
#     return decorator

def marathon_method(method: callable) -> callable:
    def decorated_method(session: 'BotSession', *args, **kwargs):
        if not session.check_marathon_created(): return
        method(session, *args, **kwargs)

    decorated_method.__name__ = method.__name__
    return decorated_method


def running_marathon_method(method: callable) -> callable:
    def decorated_method(session: 'BotSession', *args, **kwargs):
        if not session.check_marathon_created(): return
        if not session.check_marathon_running(): return
        method(session, *args, **kwargs)

    decorated_method.__name__ = method.__name__
    return decorated_method


def ongoing_operation_method(method: callable) -> callable:
    def decorated_method(session: 'BotSession', *args, **kwargs):
        if not session.operation: return
        method(session, *args, **kwargs)
        session.operation = None

    decorated_method.__name__ = method.__name__
    return decorated_method


"""Main class"""


class BotSession:
    sessions: Dict[int, 'BotSession'] = {}
    id: int
    marathon: sem.Marathon
    operation: OngoingOperation

    def __init__(self, chat_id: int):
        BotSession.sessions[chat_id] = self
        self.id = chat_id

    @staticmethod
    @cmd_handler(pass_session=False)
    def info(update: tg.Update):
        """Show info message"""
        with open('text/info.md') as text:
            update.message.reply_markdown(text.read().strip())

    @staticmethod
    @cmd_handler(pass_session=False)
    def start(update: tg.Update):
        """Start session"""
        BotSession(update.message.chat_id)
        with open('text/start.txt') as text:
            update.message.reply_text(text=text.read().strip())

    @cmd_handler()
    def shutdown(self, update: tg.Update):
        self.operation = OngoingOperation.SHUTDOWN
        update.message.reply_text("Are you sure? /yes \t /no")

    @cmd_handler('yes', pass_update=False)
    @ongoing_operation_method
    def yes(self):
        if self.operation is OngoingOperation.START_MARATHON:
            self._start_marathon()
        elif self.operation is OngoingOperation.SHUTDOWN:
            self._shutdown()

    @cmd_handler('no')
    @ongoing_operation_method
    def no(self, update: tg.Update):
        self.cancel(update)

    @cmd_handler('cancel')
    def cancel(self, update: tg.Update):
        update.message.reply_text("Cancelled the operation '{}'".format(self.operation.value))

    @cmd_handler()
    def new_marathon(self, update: tg.Update):
        """Create new marathon"""
        self.marathon = sem.Marathon()
        with open('text/new_marathon.txt') as text:
            update.message.reply_markdown(text=text.read().strip())

    @cmd_handler()
    @marathon_method
    def settings(self, update: tg.Update):
        """Show settings"""
        update.message.reply_markdown(text=self._settings_text())

    @cmd_handler(pass_args=True)
    @marathon_method
    def set_sites(self, update: tg.Update, args: List[str]):
        self.marathon.clear_sites()
        for site in args:
            try:
                self.marathon.add_site(site)
            except sem.SiteNotFoundError as err:
                self._handle_error(err)
            except sem.UserNotFoundError as err:
                self._handle_error(err)
            except sem.MultipleUsersFoundError as err:
                self._handle_error(err)

        text = '\n'.join(("Successfully set sites to:", self._sites_text()))
        update.message.reply_markdown(text=text)

    @cmd_handler(pass_args=True)
    @marathon_method
    def add_participants(self, update: tg.Update, args: List[str]):
        """Add participants to marathon"""

        def msg_lines(p: sem.Participant):
            yield "Added *{}* to marathon:".format(p.name)
            for site in self.marathon.sites:
                user = p.user(site)
                yield " - _{}_ : [user ID {}]({})".format(sem.SITES[site]['name'], user.id,
                                                          user.link)
            yield ""
            yield "Please verify the IDs are correct."

        for username in args:
            try:
                self.marathon.add_participant(username)
                update.message.reply_markdown(
                    text='\n'.join(msg_lines(self.marathon.participants[username])),
                    disable_web_page_preview=True)
            except sem.UserNotFoundError as err:
                self._handle_error(err)
            except sem.MultipleUsersFoundError as err:
                self._handle_error(err)

    # TODO: remove participant

    @cmd_handler(pass_args=True)
    @marathon_method
    def set_duration(self, update: tg.Update, args: List[str]):
        try:
            hours, minutes = 0, 0
            if len(args) == 1:
                hours = int(args[0])
            elif len(args) == 2:
                hours, minutes = int(args[0]), int(args[1])
            else:
                self._handle_error(BotArgumentError("Expected one or two argument"))

            self.marathon.duration = datetime.timedelta(hours=hours, minutes=minutes)
            update.message.reply_markdown(
                "Set the duration to *{}* (_hh:mm:ss_ )".format(self.marathon.duration))
        except ValueError:
            self._handle_error(BotArgumentError("Invalid duration given"))

    @cmd_handler(pass_args=True)
    def schedule(self, update: tg.Update, args: List[str]):
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
                self._handle_error(BotArgumentError("Expected one or two arguments"))

            date_time = datetime.datetime.combine(day, time_of_day)
            JOB_QUEUE.run_once(callback=self.start_scheduled_marathon,
                               when=date_time, context=self.id)
            update.message.reply_markdown("Scheduled marathon start for *{}*".format(date_time))
        except ValueError:
            self._handle_error(BotArgumentError("Invalid date/time given"))

    @cmd_handler()
    def start_marathon(self, update: tg.Update):
        text = '\n\n'.join(("Starting the marathon with the following settings:",
                            self._settings_text(),
                            "Continue?\t/yes\t /no"))
        self.operation = OngoingOperation.START_MARATHON
        update.message.reply_markdown(text=text)

    @cmd_handler()
    @marathon_method
    def status(self, update: tg.Update):
        update.message.reply_markdown(text=self._status_text())

    @cmd_handler()
    @marathon_method
    def leaderboard(self, update: tg.Update):
        update.message.reply_markdown(text=self._leaderboard_text())


    @job_callback()
    def send_status_update(self):
        text = '\n\n'.join((self._status_text(), self._leaderboard_text()))
        BOT.send_message(chat_id=self.id, text=text, parse_mode=ParseMode.MARKDOWN)

    @job_callback()
    def countdown(self):
        _, remaining = self.marathon.elapsed_remaining()
        seconds = int(remaining.total_seconds())
        minutes = seconds // 60
        if minutes >= 1:
            text = "*{} minutes remaining!*".format(minutes)
        else:
            text = "_*{} seconds remaining!*_".format(seconds)
        BOT.send_message(chat_id=self.id, text=text, parse_mode=ParseMode.MARKDOWN)

    @job_callback()
    def start_scheduled_marathon(self):
        self._start_marathon()


    def check_marathon_created(self) -> bool:
        if not self.marathon:
            with open('text/marathon_not_created.txt') as text:
                BOT.send_message(chat_id=self.id, text=text.read().strip())
            return False
        return True

    def check_marathon_running(self) -> bool:
        if not self.marathon.is_running:
            BOT.send_message(chat_id=self.id, text="Only available while marathon is running!")
            return False
        return True


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
                yield "\t - _{}_".format(sem.SITES[site]['name'])

        return '\n'.join(lines())

    def _participants_text(self) -> str:
        def lines():
            yield "*Sites*:"
            for site in self.marathon.sites:
                yield "\t - {}".format(sem.SITES[site]['name'])

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
            update: sem.Update = (yield)

            def per_site():
                for site, increment in update.per_site.items():
                    yield " _{}_  ({:+})".format(sem.SITES[site]['name'], increment)

            text = "*{}* just gained *{:+}* reputation on".format(update.participant, update.total)
            text += ', '.join(per_site())
            BOT.send_message(chat_id=self.id, text=text, parse_mode=ParseMode.MARKDOWN)

    def _handle_error(self, error: Exception, additional_msg: str = "", *,
                      require_action: bool = False, callback: callable = None):
        error_text = '\n\n'.join(("*ERROR*: {}".format(error), additional_msg))
        error_message = BOT.send_message(chat_id=self.id, text=error_text,
                                         parse_mode=ParseMode.MARKDOWN)

        if require_action:
            filters = tgf.Filters.chat(self.id) & reply_to_message(error_message)

            def modified_callback(bot, update):
                callback(bot, update)
                DISPATCHER.remove_handler(handler)

            handler = tge.MessageHandler(filters=filters, callback=modified_callback)
            DISPATCHER.add_handler(handler)

    def _shutdown(self):
        self.marathon.destroy()
        for job in JOB_QUEUE.jobs():
            job.schedule_removal()
        del BotSession.sessions[self.id]
        BOT.send_message(chat_id=self.id, text="I'm now sleeping. Reactivate with /start.")


def start_bot():
    import logging

    # noinspection SpellCheckingInspection
    logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                        level=logging.INFO)

    JOB_QUEUE.run_repeating(callback=save_jobs_job, interval=datetime.timedelta(minutes=1))
    try:
        load_jobs(JOB_QUEUE)
    except FileNotFoundError:
        pass

    UPDATER.start_polling()


def shutdown_bot():
    UPDATER.stop()
    for chat in BotSession.sessions:
        BOT.send_message(chat_id=chat,
                         text="*SERVER SHUTDOWN* – Going to sleep with the fishes...",
                         parse_mode=ParseMode.MARKDOWN)
    save_jobs(JOB_QUEUE)


atexit.register(shutdown_bot)

print("Done.")

if __name__ == '__main__':
    start_bot()
    UPDATER.idle()
    shutdown_bot()
