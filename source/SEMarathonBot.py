print("Initializing server... ", end='')

import telegram.ext as tgb

import marathon as sem
from utils import debug_print


TOKEN = "747763703:AAFf09Rwhmo4iIb3II0cKV43z-xmCaOofvY"

handlers = []


def cmd_handler(*, pass_session: bool = True, pass_bot: bool = False, **cmd_handler_kwargs):
    def decorator(callback: callable):
        def decorated(bot, update, *args, **kwargs):
            debug_print("/{} served".format(callback.__name__))

            session = BotSession.sessions.get(update.message.chat_id, None)
            effective_args = {
                (True, True): (session, bot, update, *args),
                (True, False): (session, update, *args),
                (False, True): (bot, update, *args),
                (False, False): (update, *args)
            }[pass_session, pass_bot]

            return callback(*effective_args, **kwargs)

        handler = tgb.CommandHandler(callback.__name__, decorated, **cmd_handler_kwargs)
        handlers.append(handler)
        return handler

    return decorator


@cmd_handler(pass_session=False)
def start(update):
    update.message.reply_markdown(text="Hi, I'm the Stack Exchange Marathon Bot! "
                                       "Type `/new_marathon` to create a new marathon.")
    BotSession(update.message.chat_id)


@cmd_handler()
def new_marathon(session, update):
    session.marathon = sem.Marathon()
    update.message.reply_markdown(text="I've created a new marathon with the default settings. "
                                       "Configure them at your will. Type `/start_marathon` "
                                       "when you're ready.")


@cmd_handler()
def settings(session: 'BotSession', update):
    if not session.marathon:
        update.message.reply_markdown(text="Marathon not yet created! "
                                           "Create one first by typing `/new_marathon`.")
        return

    def msg_lines():
        yield "Current settings for marathon:"

        yield "\n*Sites*:"
        for site in session.marathon.sites:
            yield "\t - {}".format(sem.SITE_NAMES[site])

        yield "\n*Participants*:"
        for participant in session.marathon.participants:
            yield "\t - {}".format(participant.name)

        yield "\n*Duration*: {}h".format(session.marathon.duration)

    update.message.reply_markdown(text='\n'.join(msg_lines()))


@cmd_handler(pass_args=True)
def add_participants(session, update, args):
    if not session.marathon:
        update.message.reply_markdown(text="Marathon not yet created! "
                                           "Create one first by typing `/new_marathon`.")
        return

    try:
        session.marathon.add_participants(*args)
    except sem.UserNotFoundError as err:
        update.message.reply_markdown(text="*ERROR*: {}".format(err))
    except sem.MultipleUsersFoundError as err:
        update.message.reply_markdown(text="*ERROR*: {}".format(err))

    def msg_lines(p: sem.Participant):
        yield "Added *{}* to marathon:".format(p.name)
        for site in session.marathon.sites:
            yield "\t - {}: user ID {}".format(sem.SITE_NAMES[site], p.user(site).id)
        yield ""
        yield "Please verify the IDs are correct."

    for participant in session.marathon.participants:
        update.message.reply_markdown(text='\n'.join(msg_lines(participant)))


@cmd_handler(pass_args=True)
def set_duration(session, update, args):
    pass


@cmd_handler()
def start_marathon(session, update):
    update.message.reply_text(text="Creating new marathon...")


class BotSession:
    updater = tgb.Updater(token=TOKEN)
    dispatcher = updater.dispatcher
    for handler in handlers:
        dispatcher.add_handler(handler)
    sessions = {}

    def __init__(self, chat_id: int):
        BotSession.sessions[chat_id] = self
        self.marathon = None


print("Done.")

if __name__ == '__main__':
    import logging

    logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                        level=logging.INFO)

    try:
        BotSession.updater.start_polling()
    finally:
        for chat in BotSession.sessions:
            BotSession.updater.bot.send_message(chat_id=chat,
                                                text="*SHUTDOWN* Shutting down due to error.",
                                                parse_mode='Markdown')
