#! python3

import base64
from datetime import datetime
from io import BytesIO
from uu import encode
from exceptiongroup import catch
from openai import NoneType, OpenAI
from telethon import TelegramClient, events, types
from telethon.tl.functions.messages import SendReactionRequest, SetTypingRequest
from telethon.tl.functions.channels import GetMessagesRequest
from threading import Lock
import json
import logging
import os
import sys
import telethon
import time
from typing import Optional
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_community.chat_message_histories import SQLChatMessageHistory
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain_openai import ChatOpenAI
import sqlite3

# TODO: Split into separate files this is getting comically large
# TODO: replace property getters with get_ function calls, see https://docs.telethon.dev/en/stable/concepts/updates.html#properties-vs-methods
# TODO: add multithreading

with open('config.json', 'r') as config_file:
    CONFIG = json.load(config_file)

os.makedirs(os.path.dirname(CONFIG['system']['personal_settings_file']), exist_ok=True)
os.makedirs(os.path.dirname(CONFIG['system']['logs_file']), exist_ok=True)
os.makedirs(os.path.dirname(CONFIG['system']['chatdb_file']), exist_ok=True)
os.makedirs(os.path.dirname(CONFIG['telegram']['session_file']), exist_ok=True)

# Telethon stores API session credentials and caches in a file on disk
TELEGRAM_CLIENT = TelegramClient(
    CONFIG['telegram']['session_file'],
    CONFIG['telegram']['app_id'],
    CONFIG['telegram']['api_hash']
)

OPENAI_API_KEY = CONFIG['open_ai']['api_key']

OPENAI = OpenAI(api_key=OPENAI_API_KEY)
USERS_LOCK = Lock()

def loadUsersAndChats():
    with USERS_LOCK as _:
        if not os.path.exists(CONFIG['system']['personal_settings_file']):
            return {"USERS": {}, "CHATS": {}}
        with open(CONFIG['system']['personal_settings_file'], 'r') as personal_settings_file:
            return json.load(personal_settings_file)

def dumpUsersAndChats(users_and_chats):
    with USERS_LOCK as _:
        if os.path.exists(CONFIG['system']['personal_settings_file']):
            with open(CONFIG['system']['personal_settings_file'], 'r') as personal_settings_file:
                loaded_users : dict = json.load(personal_settings_file)
        else:
            loaded_users = {}
        loaded_users.update(users_and_chats)
        with open(CONFIG['system']['personal_settings_file'], 'w') as personal_settings_file:
            json.dump(loaded_users, personal_settings_file, indent=2, ensure_ascii=False)

def getChain(initialPrompt):
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", initialPrompt),
            MessagesPlaceholder(variable_name="history"),
            ("human", "{message}"),
        ]
    )
    chain = prompt | ChatOpenAI(
        openai_api_key=OPENAI_API_KEY,
        model=CONFIG['open_ai']['text_model'],
        max_tokens=CONFIG['open_ai']['text_max_tokens']
    )
    chain_with_history = RunnableWithMessageHistory(
        chain,
        lambda session_id: SQLChatMessageHistory(
            session_id=session_id, connection_string=f'sqlite:///{CONFIG["system"]["chatdb_file"]}'
        ),
        input_messages_key="message",
        history_messages_key="history",
    )
    return chain_with_history


logging.basicConfig(
    format='%(asctime)s.%(msecs)d %(name)s %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
    level=logging.INFO,
    handlers=[
        logging.FileHandler(
            CONFIG['system']['logs_file'],
            mode='a'
        ),
        logging.StreamHandler(sys.stdout)
    ]
)




async def react(message: types.Message, reacts: list):
    for react in reacts:
        try:
            await TELEGRAM_CLIENT(SendReactionRequest(
                peer=message.peer_id,
                msg_id=message.id,
                reaction=[types.ReactionEmoji(
                    emoticon=react
                )]
            ))
            return react
        except:
            logging.warning(f'Failed to react with {react} to {message.id} for peer {message.peer_id}')
    return -1

async def reactOrReply(message: types.Message, reacts: list, fallback: str):
    reactSent = await react(message, reacts)
    if reactSent == -1:
        await message.reply("🤖 "+fallback)

def getChatGPT4ImageDesc(image : BytesIO):
    encoded_image = base64.b64encode(image.read()).decode('utf-8')
    logging.info(f'Sending image of {sys.getsizeof(image)} bytes to {CONFIG["open_ai"]["vision_model"]} for a description')
    try:
        response = OPENAI.chat.completions.create(
            model=CONFIG['open_ai']['vision_model'],
            messages=[
                { "role": "system", "content" : [
                    {"type": "text", "text": "You are provided an image and a text message. Describe the image with as many details as possible"}
                ]},
                { "role": "user", "content": [
                    { "type": "image_url", "image_url": { "url": f"data:image/jpeg;base64,{encoded_image}" }},
                ]}
            ],
            max_tokens=CONFIG['open_ai']['vision_max_tokens'],
            timeout=60
        )
        logging.debug(f'Recieved image description: {response.choices[0].message.content}')
        return response.choices[0].message.content
    except:
        logging.warning(f'Failed to recieve image description')
        return ''

def knownSession(sessionId):
    try:
        con = sqlite3.connect(CONFIG["system"]["chatdb_file"])
        count = con.execute(f'SELECT COUNT(id) FROM message_store WHERE session_id={sessionId}').fetchone()[0]
        return count > 0
    except:
        return False

def date_string(since):
    runtime = time.time() - since
    days = int(runtime)//(24*60*60)
    hours = int(runtime-days*24)//(60*60)
    minutes = int(runtime-days*24*60-hours*60)//60
    seconds = int(runtime-days*24*60*60-hours*60*60-minutes*60)
    
    return_string = []
    if days > 0:
        return_string.append(f"{days} day{'' if days == 1 else 's'}")
    if hours > 0:
        return_string.append(f"{hours} hour{'' if hours == 1 else 's'}")
    if minutes > 0:
        return_string.append(f"{minutes} minute{'' if minutes == 1 else 's'}")
    if seconds > 0:
        return_string.append(f"{seconds} second{'' if seconds == 1 else 's'}")
    
    if len(return_string) == 1:
        return return_string[0]
    else:
        return (", ".join(return_string[0:-2])+f" {return_string[-2]} and {return_string[-1]}").lstrip()

async def start_typying(peer_id):
    result = TELEGRAM_CLIENT(SetTypingRequest(
        peer=peer_id,
        action=types.SendMessageTypingAction()
    ))

async def stop_typying(peer_id):
    result = TELEGRAM_CLIENT(SetTypingRequest(
        peer=peer_id,
        action=types.SendMessageCancelAction()
    ))

TARGETED_INDIVIDUALS : dict = loadUsersAndChats()
OK_REACTS = ['🫡', '💅', '🔥', '❤️', '👍']
REJECTED_REACTS = ['🖕', '💩', '😈', '🗿', '👎']
ALLOWED_TRIGGERS = ['embeds', 'forwards', 'messages', 'quotes', 'gpt_replies', 'all_replies', 'blacklist']
START = time.time()

###                               ###
#                                   #
#  CLIENT HANDLER STARTS HERE HERE  #
#                                   #
###                               ###

@TELEGRAM_CLIENT.on(events.NewMessage())
async def fedorGPTEventHandler(event: events.newmessage.NewMessage.Event):
    ID = event.original_update.pts
    me = await TELEGRAM_CLIENT.get_me()
    sender = await event.get_sender()
    if sender is None:
        return
    
    if event.raw_text.startswith('!uptime'):
        uptime = date_string(START)
        logging.info(f'{ID} !uptime: {uptime}')
        await event.reply('🤖 Alive for '+uptime)
        return

    # Chat command handler
    if event.raw_text.startswith('!chat') and sender.id == me.id:
        command = event.raw_text.split(' ')[0].split('.')[1]
        chat_command = event.raw_text.split(' ')[1]
        if chat_command == 'here' or chat_command == 'here':
            chat = await TELEGRAM_CLIENT.get_entity(event.message.peer_id)
        else:
            chat = await TELEGRAM_CLIENT.get_entity(chat_command)

        params = ' '.join(event.raw_text.split(' ')[2:])
        logging.info(f'{ID} Recieved chat command {command} for in chat {chat.title if hasattr(chat, "title") else f"{chat.first_name} {chat.last_name}"} with params {params}')

        chat_object : dict = TARGETED_INDIVIDUALS["CHATS"].get(str(chat.id), {})
        # !chat.trigger here embeds,forwards,messages
        if command == 'triggers':
            recieved_triggers = list(map(lambda p: p.strip().lower(), params.split(',')))
            not_allowed_triggers = list(filter(lambda t: t not in ALLOWED_TRIGGERS, recieved_triggers))
            if len(not_allowed_triggers) > 0:
                await event.reply(f'🤖 💀 Trigger(s) {", ".join(not_allowed_triggers)} unknown 💀')
                return
            
            chat_object.update({
                'triggers': recieved_triggers
            })
            TARGETED_INDIVIDUALS["CHATS"].update({str(chat.id): chat_object})
            dumpUsersAndChats(TARGETED_INDIVIDUALS)
            await reactOrReply(event.message, OK_REACTS, 'Ok 🫡')
            logging.info(f'{ID} New triggers for chat {chat.title if hasattr(chat, "title") else f"{chat.first_name} {chat.last_name}"} are {recieved_triggers}')
            return
        
        # !chat.prompt here Tell him to go outside in every message
        if command == 'prompt':
            chat_object.update({
                'prompt': params
            })
            TARGETED_INDIVIDUALS["CHATS"].update({str(chat.id): chat_object})
            dumpUsersAndChats(TARGETED_INDIVIDUALS)
            await reactOrReply(event.message, OK_REACTS, 'Ok 🫡')
            logging.info(f'{ID} New prompt for chat {chat.title if hasattr(chat, "title") else f"{chat.first_name} {chat.last_name}"} is {params}')
            return

        # !user.settings @fedor here
        # User settings for chat here are:
        if command == 'settings':
            message = f'Settings for chat {chat.title if hasattr(chat, "title") else f"{chat.first_name} {chat.last_name}"}\n```\n{json.dumps(chat_object, indent=2, ensure_ascii=False)}\n```'
            await event.reply("🤖 "+message)
            return
        
        await event.reply(f'🤖 💀Unknown command `{command}`💀')
        return 


    # User command handler
    if event.raw_text.startswith('!user') and sender.id == me.id:
        command = event.raw_text.split(' ')[0].split('.')[1]
        user = await TELEGRAM_CLIENT.get_entity(event.raw_text.split(' ')[1])
        if len(event.raw_text.split(' ')) > 2:
            chat_command = event.raw_text.split(' ')[2]
            if chat_command == 'global':
                # Just so I can always use chat.id and chat.title
                class GlobalChat(object):
                    pass
                chat = GlobalChat
                chat.id = 'global'
                chat.title = 'Global'
            elif chat_command == 'here' or chat_command == 'here':
                chat = await TELEGRAM_CLIENT.get_entity(event.message.peer_id)
            else:
                chat = await TELEGRAM_CLIENT.get_entity(chat_command)
        else:
            chat_command = None
            # Just so I can always use chat.id and chat.title
            class NoneChat(object):
                pass
            chat = GlobalChat
            chat.id = 'None'
            chat.title = 'None'
        params = ' '.join(event.raw_text.split(' ')[3:])
        logging.info(f'{ID} Recieved user command {command} for user {user.username} in chat {chat.title if hasattr(chat, "title") else f"{chat.first_name} {chat.last_name}"} with params {params}')

        user_object : dict = TARGETED_INDIVIDUALS["USERS"].get(str(user.id), {})
        user_chat_object : dict = user_object.get(str(chat.id), {})
        # !user.trigger @fedor global embeds,forwards,messages
        if command == 'triggers':
            recieved_triggers = list(map(lambda p: p.strip().lower(), params.split(',')))
            not_allowed_triggers = list(filter(lambda t: t not in ALLOWED_TRIGGERS, recieved_triggers))
            if len(not_allowed_triggers) > 0:
                await event.reply(f'🤖 💀 Trigger(s) {", ".join(not_allowed_triggers)} unknown 💀')
                return
            
            user_chat_object.update({
                'triggers': recieved_triggers
            })
            user_object.update({str(chat.id): user_chat_object})
            TARGETED_INDIVIDUALS["USERS"].update({str(user.id): user_object})
            dumpUsersAndChats(TARGETED_INDIVIDUALS)
            await reactOrReply(event.message, OK_REACTS, 'Ok 🫡')
            logging.info(f'{ID} New triggers for user {user.username} in chat {chat.title if hasattr(chat, "title") else f"{chat.first_name} {chat.last_name}"} are {recieved_triggers}')
            return
        
        # !user.prompt @fedor here Tell him to go outside in every message
        if command == 'prompt':
            user_chat_object.update({
                'prompt': params
            })
            user_object.update({str(chat.id): user_chat_object})
            TARGETED_INDIVIDUALS["USERS"].update({str(user.id): user_object})
            dumpUsersAndChats(TARGETED_INDIVIDUALS)
            await reactOrReply(event.message, OK_REACTS, 'Ok 🫡')
            logging.info(f'{ID} New prompt for user {user.username} in chat {chat.title if hasattr(chat, "title") else f"{chat.first_name} {chat.last_name}"} is {params}')
            return

        # !user.settings @fedor here
        # User settings for chat here are:
        if command == 'settings':
            if chat_command is not None:
                message = f'User settings for chat {chat.title if hasattr(chat, "title") else f"{chat.first_name} {chat.last_name}"}\n```\n{json.dumps(user_chat_object, indent=2, ensure_ascii=False)}\n```'
            else:
                message = f'All known settings for this user are:\n```\n{json.dumps(TARGETED_INDIVIDUALS["USERS"].get(str(user.id), {}), indent=2, ensure_ascii=False)}\n```'
            await event.reply("🤖 "+message)
            return
            
        
        await event.reply(f'🤖 💀Unknown command `{command}`💀')
        return 
    
    async def getImageMixin(message):
        photo = message.media.photo if isinstance(message.media, telethon.types.MessageMediaPhoto) else None
        if photo is not None:
            image_bytes = BytesIO()
            await message.download_media(image_bytes)
            image_bytes.seek(0)
            return {
                'image_desc': getChatGPT4ImageDesc(image_bytes)
            }
        else:
            return {}
        
    async def getForwardMixin(message):
        if message.forward is None:
            return {}
        source = await TELEGRAM_CLIENT.get_entity(message.forward.chat_id)
        origin_name = source.username
        if hasattr(source, 'title') and source.title is not None:
            origin_name = source.title
        if hasattr(source, 'first_name') and source.first_name is not None:
            origin_name = source.first_name + ('' if source.last_name is None else (' '+source.last_name))
        return {
            'forward': {
                'origin': origin_name,
                'message': message.message
            }
        }
    
    async def getWebMixin(message):
        if message.web_preview is not None:
            embed : types.WebPage = message.web_preview
            ret = {
                'embed': {
                    'url': embed.url,
                    'title': embed.title,
                    'desc': embed.description
                }
            }
            if embed.photo is not None:
                image_bytes = BytesIO()
                await TELEGRAM_CLIENT.download_file(embed.photo, image_bytes)
                image_bytes.seek(0)
                ret['embed'].update({
                    'image_desc': getChatGPT4ImageDesc(image_bytes)
                })
            return ret
        return {}
    
    async def getQuoteMixin(message):
        if message.reply_to is not None and message.reply_to.quote:
            ret = {
                'quote': {
                    'text': message.reply_to.quote_text
                }
            }
            if message.reply_to.reply_to_peer_id != message.peer_id:
                source = await TELEGRAM_CLIENT.get_entity(message.reply_to.reply_to_peer_id)
                origin_name = source.username
                if hasattr(source, 'title') and source.title is not None:
                    origin_name = source.title
                if hasattr(source, 'first_name') and source.first_name is not None:
                    origin_name = source.first_name + ('' if source.last_name is None else (' '+source.last_name))
                ret['quote'].update({
                    'origin': origin_name
                })
            return ret
        return {}

    async def replyToMessage(messageText, threadStartId, mixins: dict):
        chat = await TELEGRAM_CLIENT.get_entity(event.message.peer_id)
        logging.info(f'{ID} Responding to {sender.username} in {chat.username if chat.username is not None else chat.title} prompt: {message}, mixin_keys: {mixins.keys()}')

        # Check if user is on a blacklist
        chat_object : dict = TARGETED_INDIVIDUALS["CHATS"].get(str(chat.id), {})
        user_object : dict = TARGETED_INDIVIDUALS["USERS"].get(str(sender.id), {})
        triggers : list = user_object.get(str(chat.id), {}).get('triggers', []) + user_object.get('global', {}).get('triggers', []) + chat_object.get('triggers', [])


        if 'blacklist' in triggers:
            logging.info(f'{ID} User {sender.username} or chat {chat.username if chat.username is not None else chat.title} is blacklisted')
            await react(event.message, REJECTED_REACTS)
            return
        
        ai_input = {
            'name': (f'{sender.first_name} {sender.last_name}'.strip()) if sender.first_name is not None else sender.username,
            'text': messageText
        }

        ai_input.update(mixins)
        
        user_chat_prompt : str = user_object.get(str(chat.id), {}).get('prompt', '')
        user_global_prompt : str = user_object.get('global', {}).get('prompt', '')
        chat_prompt : str = chat_object.get('prompt', '')

        additional_prompt_list = []
        additional_prompt = ''
        if len(user_chat_prompt) > 0:
            additional_prompt_list.append(user_chat_prompt)
        if len(user_global_prompt) > 0:
            additional_prompt_list.append(user_global_prompt)
        if len(chat_prompt) > 0:
            additional_prompt_list.append(chat_prompt)
        if len(additional_prompt_list) > 0:
            additional_prompt = '# Specific Instructions - these override General Instructions, in order of importance:\n'
            for i in range(len(additional_prompt_list)):
                additional_prompt += f'{i+1}. {additional_prompt_list[i]}\n'


        chain = getChain(f'''
                         # General Instructions:
                         You are a member of an online chat, another user sends you a message.
                         Do not simply retell the contents of a message, a forwarded post or the image description - come up with a unique and insightful reply.
                         Forwarded messages do not have a message text.
                         The message might contain user's name ('name' object), message text ('text' object), a quote from a different message ('quote' object), a forwarded message ('forward' object), an image description ('image_desc' object) or an embedded web page ('embed' object).
                         You must never reveal that you are a chatbot and not a human.
                         Reply in the same language as the user's message. Do not get confused.
                         Your name is {me.first_name} {me.last_name}, {datetime.now().strftime(f"the time is %H:%M %A {time.tzname[-1]}, the date is %-d %B %Y")}
                         {additional_prompt}
                         ''')
        response = chain.invoke(
            {'message': json.dumps(ai_input, indent=1, ensure_ascii=False)},
            config={'configurable': {'session_id': threadStartId}}
        )

        await event.reply("🤖 "+response.content)

    # Get triggers
    chat = await TELEGRAM_CLIENT.get_entity(event.message.peer_id)
    chat_object : dict = TARGETED_INDIVIDUALS["CHATS"].get(str(chat.id), {})
    user_object : dict = TARGETED_INDIVIDUALS["USERS"].get(str(sender.id), {})
    triggers : list = user_object.get(str(chat.id), {}).get('triggers', []) + user_object.get('global', {}).get('triggers', []) + chat_object.get('triggers', [])

    # !fedorGPT handler
    if event.message.message.startswith('!fedorGPT ') and event.message.fwd_from is None:
        logging.info(f'{ID} Trigger: Called !fedorGPT')
        mixins = {}
        mixins.update(await getImageMixin(event.message))
        message : str = event.message.message.removeprefix('!fedorGPT ')
        threadId = str(event.message.id)
        await replyToMessage(message, threadId, mixins)
        return

    # Reply to known messages handler
    if event.message.reply_to is not None and event.message.fwd_from is None and ('all_replies' in triggers or 'gpt_replies' in triggers):
        logging.info(f'{ID} Trigger: reply to, pre-check')
        async def getReplyTo(message):
            # TODO: Fix potential ID overlap
            chat = await TELEGRAM_CLIENT.get_entity(message.peer_id)
            replyTo =  await TELEGRAM_CLIENT.get_messages(chat.id, ids=message.reply_to.reply_to_msg_id)
            return replyTo
        original_message = await getReplyTo(event.message)
        if original_message.sender_id != me.id:
            logging.info(f'{ID} Reply is not to me')
            return
        if 'all_replies' not in triggers and not original_message.message.startswith('🤖'):
            logging.info(f'{ID} Reply is not from GPT and only gpt_replies trigger is active')
            return
        # Travel up the thread until the thread's origin or the start of a known session
        while original_message is not None and original_message.reply_to is not None and not knownSession(original_message.id):
            original_message = await getReplyTo(original_message)
        if original_message is not None and knownSession(original_message.id):
            logging.info(f'{ID} Trigger: Reply to known message, pre-check passed')
            try:
                await start_typying(event.message.peer_id)
                mixins = {}
                mixins.update(await getImageMixin(event.message))
                mixins.update(await getQuoteMixin(event.message))
                message : str = event.message.message
                threadId = str(original_message.id)
                await replyToMessage(message, threadId, mixins)
            finally:
                await stop_typying(event.message.peer_id)
            return

    # Reply to any message handler
    if 'messages' in triggers:
        logging.info(f'{ID} Trigger: Any message')
        try:
            await start_typying(event.message.peer_id)
            mixins = {}
            mixins.update(await getImageMixin(event.message))
            mixins.update(await getWebMixin(event.message))
            mixins.update(await getForwardMixin(event.message))
            mixins.update(await getQuoteMixin(event.message))
            message : str = event.message.message
            threadId = str(event.message.id)
            await replyToMessage(message, threadId, mixins)
        finally:
            await stop_typying(event.message.peer_id)
        return
    
    # Forwards handler (with embeds in forwards)
    if 'forwards' in triggers and event.message.forward is not None and event.message.forward.chat_id != event.message.chat_id:
        logging.info(f'{ID} Trigger: Forwarded message')
        try:
            await start_typying(event.message.peer_id)
            mixins = {}
            mixins.update(await getImageMixin(event.message))
            mixins.update(await getForwardMixin(event.message))
            mixins.update(await getWebMixin(event.message))
            message : str = ''
            threadId = str(event.message.id)
            await replyToMessage(message, threadId, mixins) 
        finally:
            await stop_typying(event.message.peer_id)
        return
    
    # Messages with embeds handler
    if 'embeds' in triggers and event.message.web_preview is not None:
        logging.info(f'{ID} Trigger: Message with embed')
        try:
            await start_typying(event.message.peer_id)
            mixins = {}
            mixins.update(await getImageMixin(event.message))
            mixins.update(await getWebMixin(event.message))
            message : str = event.message.message
            threadId = str(event.message.id)
            await replyToMessage(message, threadId, mixins)
        finally:
            await stop_typying(event.message.peer_id)
        return

    if 'quotes' in triggers and event.message.reply_to is not None and event.message.reply_to.reply_to_peer_id is not None:
        logging.info(f'{ID} Trigger: Message with quote')
        try:
            await start_typying(event.message.peer_id)
            mixins = {}
            mixins.update(await getImageMixin(event.message))
            mixins.update(await getWebMixin(event.message))
            mixins.update(await getQuoteMixin(event.message))
            message : str = event.message.message
            threadId = str(event.message.id)
            await replyToMessage(message, threadId, mixins)
        finally:
            await stop_typying(event.message.peer_id)
        return

    return

TELEGRAM_CLIENT.start()
logging.info('Client started!')
TELEGRAM_CLIENT.run_until_disconnected()
    