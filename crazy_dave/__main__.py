import json
import os
import random
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telethon import TelegramClient, connection, events
from telethon.tl.custom import Message as _Message
from telethon.tl.types import InputPeerChat, PeerChat, SendMessageTypingAction, User

from .logger import MessageLogger
from .oss import Uploader
from .predictor import Predictor
from .utils import MaxSizeDict

api_id = int(os.environ["API_ID"])
api_hash = os.environ["API_HASH"]
bot_token = os.environ["BOT_TOKEN"]
group_id = os.environ["GROUP_ID"]
lstm_url = os.environ["LSTM_URL"]
s2s_url = os.environ["S2S_URL"]
oss_endpoint = os.environ["OSS_ENDPOINT"]
oss_bucket = os.environ["OSS_BUCKET"]
aliyun_accesskey_id = os.environ["ALIYUN_ACCESSKEY_ID"]
aliyun_accesskey_secret = os.environ["ALIYUN_ACCESSKEY_SECRET"]
mtproto_server = os.environ.get("MTPROTO_SERVER", None)
mtproto_port = int(os.environ.get("MTPROTO_PORT", 0))
mtproto_secret = os.environ.get("MTPROTO_SECRET", None)

use_proxy = mtproto_server and mtproto_port and mtproto_secret
if use_proxy:
    bot = TelegramClient("bot", api_id, api_hash,
                         connection=connection.ConnectionTcpMTProxyIntermediate,
                         proxy=(mtproto_server, mtproto_port, mtproto_secret))
else:
    bot = TelegramClient("bot", api_id, api_hash)

scheduler = AsyncIOScheduler()
predictor = Predictor(s2s_url, lstm_url)
uploader = Uploader((aliyun_accesskey_id, aliyun_accesskey_secret), oss_endpoint, oss_bucket)
logger = MessageLogger()
responses = MaxSizeDict(128)

me: Optional[User] = None
group: Optional[InputPeerChat] = None
chance: float = 0.1


@bot.on(events.NewMessage(pattern="/blame"))
async def blame(event: events.NewMessage.Event):
    if event.is_reply:
        reply_message: _Message = await event.get_reply_message()
        reply_user: User = reply_message.sender
        if reply_user == me and (rsp := responses.get(reply_message.id, None)):
            # noinspection PyUnboundLocalVariable
            await event.reply(json.dumps(rsp, ensure_ascii=False))
        else:
            await event.reply("Log rotated.")
    elif responses:
        await event.reply(json.dumps(list(responses.values())[-1], ensure_ascii=False))
    raise events.StopPropagation


# noinspection PyTypeChecker
@bot.on(events.NewMessage)
async def new_message(event: events.NewMessage.Event):
    message: _Message = event.message
    if not (isinstance(message.to_id, PeerChat) and message.to_id.chat_id == group.chat_id):
        return
    logger.log(message)

    global responses
    if event.is_reply:
        reply_message: _Message = await event.get_reply_message()
        reply_user: User = reply_message.sender
        if reply_user == me:
            async with bot.action(group, SendMessageTypingAction()):
                sentence = [reply_message.text, message.text] if random.random() < 0.5 else message.text
                rsp, raw = await predictor.predict(sentence)
                req_id = await event.reply(rsp)
                responses[req_id.id] = raw
    elif random.random() < chance or message.text.startswith(f"@{me.username}"):
        async with bot.action(group, SendMessageTypingAction()):
            sentence = [msg.text for msg in
                    logger.last_messages(5)] if random.random() < 0.5 else logger.last_message.text
            rsp, raw = await predictor.predict(sentence)
            req_id = await bot.send_message(group, rsp)
            responses[req_id.id] = raw


@scheduler.scheduled_job("interval", minutes=10)
async def model_update():
    lstm_result, s2s_result = await predictor.update_model()
    if lstm_result.updated:
        await bot.send_message(group,
                               f"Legacy model updated.\n{lstm_result.old_version} -> {lstm_result.current_version}")
    if s2s_result.updated:
        await bot.send_message(group, f"S2S model updated.\n{s2s_result.old_version} -> {s2s_result.current_version}")


@scheduler.scheduled_job("interval", minutes=10)
async def history_upload():
    await uploader.upload(logger.dumps())


async def startup():
    global me, group
    me = await bot.get_me()
    group = await bot.get_input_entity(group_id)


async def shutdown():
    await predictor.close()


bot.start(bot_token=bot_token)
scheduler.start()
print("Bot started.")
bot.loop.run_until_complete(startup())
try:
    bot.run_until_disconnected()
finally:
    print("Bot shutdown.")
    bot.loop.run_until_complete(shutdown())
