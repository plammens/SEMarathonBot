from typing import List, Dict

print("Initializing server... ", end='')

import telegram as tg
import telegram.ext as tge

import marathon as sem
from utils import debug_print


with open('token.txt') as file:
    TOKEN: str = file.read().strip()

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


# TODO: extract check marathon method


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
    def start(update: tg.Update):
        update.message.reply_markdown(text="Hi, I'm the Stack Exchange Marathon Bot! "
                                           "Type `/new_marathon` to create a new marathon.")
        BotSession(update.message.chat_id)


    @cmd_handler()
    def new_marathon(self, update: tg.Update):
        self.marathon = sem.Marathon()
        update.message.reply_markdown(text="I've created a new marathon with the default settings. "
                                           "Configure them at your will. Type `/start_marathon` "
                                           "when you're ready.")


    @cmd_handler()
    def settings(self, update: tg.Update):
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
        if not self.marathon_created(): return

        try:
            self.marathon.add_participants(*args)
        except sem.UserNotFoundError as err:
            update.message.reply_markdown(text="*ERROR*: {}".format(err))
        except sem.MultipleUsersFoundError as err:
            update.message.reply_markdown(text="*ERROR*: {}".format(err))

        def msg_lines(p: sem.Participant):
            yield "Added *{}* to marathon:".format(p.name)
            for site in self.marathon.sites:
                yield "\t - {}: user ID {}".format(sem.SITE_NAMES[site], p.user(site).id)
            yield ""
            yield "Please verify the IDs are correct."

        for name in args:
            update.message.reply_markdown(text='\n'.join(msg_lines(self.marathon.participants[name])))


    @cmd_handler(pass_args=True)
    def set_duration(self, update: tg.Update, args: List[str]):
        pass


    @cmd_handler()
    def start_marathon(self, update: tg.Update, ):
        update.message.reply_text(text="Creating new marathon...")


    def marathon_created(self) -> bool:
        if not self.marathon:
            BOT.send_message(chat_id=self.id,
                             text="Marathon not yet created! "
                                  "Create one first by typing /new_marathon .")
            return False
        return True


print("Done.")

if __name__ == '__main__':
    import logging

    logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                        level=logging.INFO)

    UPDATER.start_polling()
