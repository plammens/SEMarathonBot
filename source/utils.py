import telegram.ext.filters


def debug_print(msg: str):
    print('\t'.join(('[SEMB]', msg)))


class reply_to_message(telegram.ext.filters.BaseFilter):
    message_id: int

    def __init__(self, message: telegram.Message):
        self.message_id = message.message_id

    def filter(self, message: telegram.Message):
        return message.reply_to_message.message_id == self.message_id


