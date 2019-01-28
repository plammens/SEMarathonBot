from typing import List, Dict

print("Initializing server... ", end='')

import telegram as tg
import telegram.ext as tge
import telegram.ext.filters as tgf
from telegram.parsemode import ParseMode
from enum import Enum

import marathon as sem
from utils import *


with open('token.txt') as token_file:
    TOKEN: str = token_file.read().strip()

UPDATER = tge.Updater(token=TOKEN)
BOT, DISPATCHER = UPDATER.bot, UPDATER.dispatcher

"""Helper classes"""

class BotArgumentError(ValueError):
    pass


class OngoingOperation(Enum):
    START_MARATHON = "start marathon"


"""Method decorators"""

def cmd_handler(cmd: str = None, *, pass_session: bool = True, pass_bot: bool = False,
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
            effective_args = {
                (True, True):   (session, bot, update, *args),
                (True, False):  (session, update, *args),
                (False, True):  (bot, update, *args),
                (False, False): (update, *args)
            }[pass_session, pass_bot]

            debug_print("/{} served".format(callback.__name__))
            return callback(*effective_args, **kwargs)

        handler = tge.CommandHandler(cmd, decorated, **cmd_handler_kwargs)
        DISPATCHER.add_handler(handler)
        return callback

    return decorator


def marathon_method(method: callable) -> callable:
    def decorated_method(session: 'BotSession', *args, **kwargs):
        if not session.marathon_created(): return
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
        self.marathon = None
        self.operation = None

    @staticmethod
    @cmd_handler(pass_session=False)
    def info(update: tg.Update):
        """Show info message"""
        with open('text/info.md') as file:
            update.message.reply_markdown(file.read().strip())

    @staticmethod
    @cmd_handler(pass_session=False)
    def start(update: tg.Update):
        """Start session"""
        BotSession(update.message.chat_id)
        with open('text/start.txt') as file:
            update.message.reply_text(text=file.read().strip())

    @cmd_handler('yes')
    @ongoing_operation_method
    def yes(self, update: tg.Update):
        if self.operation is OngoingOperation.START_MARATHON:
            self.marathon.start()

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
        with open('text/new_marathon.txt') as file:
            update.message.reply_markdown(text=file.read().strip())

    @cmd_handler()
    @marathon_method
    def settings(self, update: tg.Update):
        """Show settings"""
        update.message.reply_markdown(text=self.settings_msg())

    @cmd_handler(pass_args=True)
    @marathon_method
    def add_participants(self, update: tg.Update, args: List[str]):
        """Add participants to marathon"""

        def msg_lines(p: sem.Participant):
            yield "Added *{}* to marathon:".format(p.name)
            for site in self.marathon.sites:
                user = p.user(site)
                yield " - _{}_ : [user ID {}]({})".format(sem.SITES[site]['name'], user.id, user.link)
            yield ""
            yield "Please verify the IDs are correct."

        for username in args:
            try:
                self.marathon.add_participant(username)
                update.message.reply_markdown(text='\n'.join(msg_lines(self.marathon.participants[username])),
                                              disable_web_page_preview=True)
            except sem.UserNotFoundError as err:
                self.handle_error(err)
            except sem.MultipleUsersFoundError as err:
                self.handle_error(err)

    @cmd_handler(pass_args=True)
    @marathon_method
    def set_duration(self, update: tg.Update, args: List[str]):
        if len(args) != 1: self.handle_error(BotArgumentError("Expected only one argument"))
        try:
            duration = int(args[0])
            self.marathon.duration = duration
            update.message.reply_markdown("Set the duration to *{}h*".format(duration))
        except ValueError:
            self.handle_error(BotArgumentError("Invalid duration given"))

    @cmd_handler()
    def start_marathon(self, update: tg.Update):
        # TODO: start marathon backend
        text = '\n\n'.join(("Starting the marathon with the following settings:",
                            self.settings_msg(),
                            "Continue?\t/yes\t /no"))
        self.operation = OngoingOperation.START_MARATHON
        update.message.reply_markdown(text=text)


    def marathon_created(self) -> bool:
        if not self.marathon:
            with open('text/marathon_not_created.txt') as file:
                BOT.send_message(chat_id=self.id, text=file.read().strip())
            return False
        return True

    def settings_msg(self) -> str:
        def lines():
            yield "Current settings for marathon:"

            yield "\n*Sites*:"
            for site in self.marathon.sites:
                yield "\t - {}".format(sem.SITES[site]['name'])

            yield "\n*Participants*:"
            for participant in self.marathon.participants.values():
                yield "\t - {}".format(participant.name)

            yield "\n*Duration*: {}h".format(self.marathon.duration)

        return '\n'.join(lines())

    def handle_error(self, error: Exception, additional_msg: str = "", *,
                     require_action: bool = False, callback: callable = None):
        error_text = '\n\n'.join(("*ERROR*: {}".format(error), additional_msg))
        error_message = BOT.send_message(chat_id=self.id, text=error_text, parse_mode=ParseMode.MARKDOWN)

        if require_action:
            filters = tgf.Filters.chat(self.id) & reply_to_message(error_message)

            def modified_callback(bot, update):
                callback(bot, update)
                DISPATCHER.remove_handler(handler)

            handler = tge.MessageHandler(filters=filters, callback=modified_callback)
            DISPATCHER.add_handler(handler)


print("Done.")

if __name__ == '__main__':
    import logging

    logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                        level=logging.INFO)

    UPDATER.start_polling()
