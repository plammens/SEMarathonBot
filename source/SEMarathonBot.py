from typing import List, Dict

print("Initializing server... ", end='')

import telegram as tg
import telegram.ext as tge

import marathon as sem
from utils import debug_print


with open('token.txt') as token_file:
    TOKEN: str = token_file.read().strip()

UPDATER = tge.Updater(token=TOKEN)
BOT, DISPATCHER = UPDATER.bot, UPDATER.dispatcher


def cmd_handler(*, pass_session: bool = True, pass_bot: bool = False, **cmd_handler_kwargs) -> callable:
    """Returns specialized decorator for CommandHandler callback functions"""

    def decorator(callback: callable) -> tge.CommandHandler:
        """Actual decorator"""

        def decorated(bot, update, *args, **kwargs):
            debug_print("/{} served".format(callback.__name__))

            session = BotSession.sessions.get(update.message.chat_id, None)
            if pass_session and session is None: return
            effective_args = {
                (True, True): (session, bot, update, *args),
                (True, False): (session, update, *args),
                (False, True): (bot, update, *args),
                (False, False): (update, *args)
            }[pass_session, pass_bot]

            return callback(*effective_args, **kwargs)

        handler = tge.CommandHandler(callback.__name__, decorated, **cmd_handler_kwargs)
        DISPATCHER.add_handler(handler)
        return handler

    return decorator


class BotSession:
    sessions: Dict[int, 'BotSession'] = {}
    id: int
    marathon: sem.Marathon

    def __init__(self, chat_id: int):
        BotSession.sessions[chat_id] = self
        self.id = chat_id
        self.marathon = None

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

    @cmd_handler()
    def new_marathon(self, update: tg.Update):
        """Create new marathon"""
        self.marathon = sem.Marathon()
        with open('text/new_marathon.txt') as file:
            update.message.reply_markdown(text=file.read().strip())

    @cmd_handler()
    def settings(self, update: tg.Update):
        """Show settings"""
        if not self.marathon_created(): return

        def msg_lines():
            yield "Current settings for marathon:"

            yield "\n*Sites*:"
            for site in self.marathon.sites:
                yield "\t - {}".format(sem.SITE_NAMES[site])

            yield "\n*Participants*:"
            for participant in self.marathon.participants.values():
                yield "\t - {}".format(participant.name)

            yield "\n*Duration*: {}h".format(self.marathon.duration)

        update.message.reply_markdown(text='\n'.join(msg_lines()))

    @cmd_handler(pass_args=True)
    def add_participants(self, update: tg.Update, args: List[str]):
        """Add participants to marathon"""
        if not self.marathon_created(): return

        def msg_lines(p: sem.Participant):
            yield "Added *{}* to marathon:".format(p.name)
            for site in self.marathon.sites:
                yield "\t - {}: user ID {}".format(sem.SITE_NAMES[site], p.user(site).id)
            yield ""
            yield "Please verify the IDs are correct."

        for username in args:
            try:
                self.marathon.add_participant(username)
                update.message.reply_markdown(text='\n'.join(msg_lines(self.marathon.participants[username])))
            except sem.UserNotFoundError as err:
                self.send_error_message(err)
            except sem.MultipleUsersFoundError as err:
                self.send_error_message(err)


    @cmd_handler(pass_args=True)
    def set_duration(self, update: tg.Update, args: List[str]):
        pass

    @cmd_handler()
    def start_marathon(self, update: tg.Update, ):
        update.message.reply_text(text="Creating new marathon...")

    def marathon_created(self) -> bool:
        if not self.marathon:
            with open('marathon_not_created.txt') as file:
                BOT.send_message(chat_id=self.id, text=file.read().strip())
            return False
        return True

    def send_error_message(self, error: Exception):
        BOT.send_message(chat_id=self.id, text="*ERROR*: {}".format(error))


print("Done.")

if __name__ == '__main__':
    import logging

    logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                        level=logging.INFO)

    UPDATER.start_polling()
