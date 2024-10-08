from __future__ import annotations

import asyncio
import logging
import re
import os
import pandas as pd
import datetime as datetime
import aiogram

from aiogram import Bot, Dispatcher, types
from aiogram.types import LabeledPrice
from uuid import uuid4
from telegram import BotCommandScopeAllGroupChats, Update, constants
from telegram import InlineKeyboardMarkup, InlineKeyboardButton, InlineQueryResultArticle
from telegram import InputTextMessageContent, BotCommand
from telegram.error import RetryAfter, TimedOut
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, \
    filters, InlineQueryHandler, CallbackQueryHandler, Application, ContextTypes, CallbackContext

from pydub import AudioSegment

from utils import is_group_chat, get_thread_id, message_text, wrap_with_indicator, split_into_chunks, \
    edit_message_with_retry, get_stream_cutoff_values, is_allowed, get_remaining_budget, is_admin, is_within_budget, \
    get_reply_to_message_id, add_chat_request_to_usage_tracker, error_handler, is_direct_result, handle_direct_result, \
    cleanup_intermediate_files, is_allowed_prem
from openai_helper import OpenAIHelper, localized_text
from usage_tracker import UsageTracker


class ChatGPTTelegramBot:
    """
    Class representing a ChatGPT Telegram Bot.
    """

    def __init__(self, config: dict, openai: OpenAIHelper):
        """
        Initializes the bot with the given configuration and GPT bot object.
        :param config: A dictionary containing the bot configuration
        :param openai: OpenAIHelper object
        """
        
        self.config = config
        self.openai = openai
        bot_language = self.config['bot_language']
        self.commands = [
            BotCommand(command='help', description=localized_text('help_description', bot_language)),
            BotCommand(command='reset', description=localized_text('reset_description', bot_language)),
            BotCommand(command='booksearch', description=localized_text('Book_Selection', bot_language)),
            BotCommand(command='bookretell', description=localized_text('Book_Retelling', bot_language)),
            BotCommand(command='bookmatch', description=localized_text('Book_Match', bot_language)),
            BotCommand(command='booktalk', description=localized_text('booktalk', bot_language)),
            BotCommand(command='premium', description='Показать условия премиума')
            
        ]
        # If imaging is enabled, add the "image" command to the list
        '''if self.config.get('enable_image_generation', False):
            self.commands.append(BotCommand(command='image', description=localized_text('image_description', bot_language)))'''

        self.group_commands = [BotCommand(
            command='chat', description=localized_text('chat_description', bot_language)
        )]
        self.disallowed_message = localized_text('disallowed', bot_language)
        self.budget_limit_message = localized_text('budget_limit', bot_language)
        self.usage = {}
        self.last_message = {}
        self.inline_queries_cache = {}
        self.userflag = []
        self.cooldown = []
        self.user_requests = {} #для лимитов сообщений
        

    async def help(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Shows the help menu.
        """

        bot_language = self.config['bot_language']
        help_text = (
                localized_text('help_text', bot_language)[0] +
                '\n\n' +
                localized_text('help_text', bot_language)[1] +
                '\n\n' +
                localized_text('help_text', bot_language)[2]
        )
        await update.message.reply_text(help_text, disable_web_page_preview=True)
        
    
    async def premium(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.effective_message.reply_text(
            message_thread_id=get_thread_id(update),
            text=localized_text('premium', self.config['bot_language'])
        )
    
    async def buy(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await is_allowed_prem(self.config, update, context):
            user_name = update.message.from_user.name
            mes = user_name + ' - хочет приобрести премиум!'
            await update.effective_message.reply_text(
                message_thread_id=get_thread_id(update),
                text=localized_text('buy', self.config['bot_language'])
                )
            await context.bot.send_message('449482825',mes)
        else:
            await update.effective_message.reply_text(
                message_thread_id=get_thread_id(update),
                text= 'У вас уже подключена премиум версия бота!')
                
        
    async def addPrem(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_name = update.message.from_user.name
        if user_name == '@Elchin_ka':
            
            name_prem = message_text(update.message)
            logging.info(name_prem)
            file_path = "/LibAI/pycharmProject/bot/premUser.xlsx"
            df = pd.read_excel(file_path)
            new_row = {'User': name_prem,
                       'Date Added': datetime.datetime.now()}

            data_to_append = [new_row]

            # Преобразуем список словарей в DataFrame и объединяем его с существующим DataFrame
            new_df = pd.DataFrame(data_to_append)
            df = pd.concat([df, new_df], ignore_index=True)
            
            # Сохраняем обновленный DataFrame в Excel-файл
            df.to_excel(file_path, index=False)
            await update.effective_message.reply_text(
                message_thread_id=get_thread_id(update),
                text= 'Имя ' + name_prem +' успешно сохраненно'
            )
            df = pd.read_excel(file_path)
            logging.info(df)
            return
                
        

    async def limit( self, user_name, max_requests_per_day) -> bool:
        
        current_time = datetime.datetime.now()
        

        if user_name in self.user_requests:
            first_request_time, request_count = self.user_requests[user_name]
            
            # Проверяем, начался ли новый 24-часовой период
            if current_time - first_request_time > datetime.timedelta(days=2):
                # Начался новый период, сбрасываем счетчик
                self.user_requests[user_name] = (current_time, 1)
                
            else:
                # Текущий период, увеличиваем счетчик запросов, если не превышен лимит
                if request_count < max_requests_per_day:
                    self.user_requests[user_name] = (first_request_time, request_count + 1)
                    
                else:
                    # Лимит запросов достигнут, сообщаем пользователю
                    return False
        else:
            # Первый запрос пользователя
            self.user_requests[user_name] = (current_time, 1)
            
        return True

    async def reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Resets the conversation.
        """

        if not await is_allowed(self.config, update, context):
            logging.warning(f'User {update.message.from_user.name} (id: {update.message.from_user.id}) '
                            f'is not allowed to reset the conversation')
            await self.send_disallowed_message(update, context)
            return

        logging.info(f'Resetting the conversation for user {update.message.from_user.name} '
                     f'(id: {update.message.from_user.id})...')

        chat_id = update.effective_chat.id
        reset_content = message_text(update.message)
        self.openai.reset_chat_history(chat_id=chat_id, content=reset_content)
        await update.effective_message.reply_text(
            message_thread_id=get_thread_id(update),
            text=localized_text('reset_done', self.config['bot_language'])
        )
        user_id = update.message.from_user.id
        image_query = message_text(update.message)

        if user_id in self.cooldown:
            self.cooldown.remove(user_id)
        if user_id in self.userflag:
            index = self.userflag.index(user_id) - 1
            self.userflag.pop(index)
            self.userflag.remove(user_id)

    async def bookretell(self, update: Update, context: ContextTypes.DEFAULT_TYPE):

        if not await is_allowed(self.config, update, context):
            logging.warning(f'User {update.message.from_user.name} (id: {update.message.from_user.id}) '
                            f'is not allowed to reset the conversation')
            await self.send_disallowed_message(update, context)
            return
        
        if not await is_allowed_prem(self.config, update, context):
            
            user_name = update.message.from_user.name
            max_requests_per_day = 15
            if not await self.limit(user_name, max_requests_per_day):

                await update.effective_message.reply_text(
                                 message_thread_id=get_thread_id(update),
                                 text=localized_text('limit', self.config['bot_language']))
                                 
                return

        user_id = update.message.from_user.id
        image_query = message_text(update.message)
        if user_id in self.userflag:
            index = self.userflag.index(user_id) - 1
            self.userflag.pop(index)
            self.userflag.remove(user_id)
        if image_query == '':
             self.userflag.append(1)
             self.userflag.append(user_id)
             await update.effective_message.reply_text(
                message_thread_id=get_thread_id(update),
                text=localized_text('retell', self.config['bot_language'])
            )
             return
        else:
            await update.effective_message.reply_text(
                message_thread_id=get_thread_id(update),
                text=localized_text('command_with_promnt', self.config['bot_language'])
            )

        await wrap_with_indicator(update, context, constants.ChatAction.TYPING)

    async def booksearch(self, update: Update, context: ContextTypes.DEFAULT_TYPE):

        if not await is_allowed(self.config, update, context):
            logging.warning(f'User {update.message.from_user.name} (id: {update.message.from_user.id}) '
                            f'is not allowed to reset the conversation')
            await self.send_disallowed_message(update, context)
            return

        if not await is_allowed_prem(self.config, update, context):
            
            user_name = update.message.from_user.name
            max_requests_per_day = 15
            if not await self.limit(user_name, max_requests_per_day):

                await update.effective_message.reply_text(
                                 message_thread_id=get_thread_id(update),
                                 text=localized_text('limit', self.config['bot_language']))
                                 
                return

        user_id = update.message.from_user.id
        image_query = message_text(update.message)
        if user_id in self.userflag:
            index = self.userflag.index(user_id) - 1
            self.userflag.pop(index)
            self.userflag.remove(user_id)
        if image_query == '':
             self.userflag.append(2)
             self.userflag.append(user_id)
             await update.effective_message.reply_text(
                message_thread_id=get_thread_id(update),
                text=localized_text('search', self.config['bot_language'])
            )
             return

        else:
            await update.effective_message.reply_text(
                message_thread_id=get_thread_id(update),
                text=localized_text('command_with_promnt', self.config['bot_language'])
            )

        await wrap_with_indicator(update, context, constants.ChatAction.TYPING)

    async def bookmatch(self, update: Update, context: ContextTypes.DEFAULT_TYPE):

        if not await is_allowed(self.config, update, context):
            logging.warning(f'User {update.message.from_user.name} (id: {update.message.from_user.id}) '
                            f'is not allowed to reset the conversation')
            await self.send_disallowed_message(update, context)
            return
        
        user_id = update.message.from_user.id
        image_query = message_text(update.message)
        if user_id in self.userflag:
            index = self.userflag.index(user_id) - 1
            self.userflag.pop(index)
            self.userflag.remove(user_id)
        if image_query == '':
             self.userflag.append(4)
             self.userflag.append(user_id)
             await update.effective_message.reply_text(
                message_thread_id=get_thread_id(update),
                text=localized_text('match', self.config['bot_language'])
            )
             return

        else:
            await update.effective_message.reply_text(
                message_thread_id=get_thread_id(update),
                text=localized_text('command_with_promnt', self.config['bot_language'])
            )



    async def gpt(self, update: Update, context: ContextTypes.DEFAULT_TYPE):

        if not await is_allowed(self.config, update, context):
            logging.warning(f'User {update.message.from_user.name} (id: {update.message.from_user.id}) '
                            f'is not allowed to reset the conversation')
            await self.send_disallowed_message(update, context)
            return
        if not await is_allowed_prem(self.config, update, context):
            await update.effective_message.reply_text(
                message_thread_id=get_thread_id(update),
                text='Команда не доступна!\nЧтобы задать вопрос, подключите /premium.'
            )
            return
        
        user_id = update.message.from_user.id
        image_query = message_text(update.message)
        if user_id in self.userflag:
            index = self.userflag.index(user_id) - 1
            self.userflag.pop(index)
            self.userflag.remove(user_id)
        if image_query == '':
             self.userflag.append(5)
             self.userflag.append(user_id)
             await update.effective_message.reply_text(
                message_thread_id=get_thread_id(update),
                text=localized_text('booktalk_text', self.config['bot_language'])
            )
             return
         
    

    async def prompt(self, update: Update, context: ContextTypes.DEFAULT_TYPE):

        if not await is_allowed(self.config, update, context):
            logging.warning(f'User {update.message.from_user.name} (id: {update.message.from_user.id}) '
                            f'is not allowed to reset the conversation')
            await self.send_disallowed_message(update, context)
            return
        """
        React to incoming messages and respond accordingly.
        """
        if update.edited_message or not update.message or update.message.via_bot:
            return

        if not await self.check_allowed_and_within_budget(update, context):
            return

        user_id = update.message.from_user.id




        logging.info(
            f'New message received from user {update.message.from_user.name} (id: {update.message.from_user.id})')
        chat_id = update.effective_chat.id

        if user_id in self.userflag:
                flag = self.userflag[self.userflag.index(user_id) - 1]
                if flag ==1:
                    prompt = 'Кратко пересскажи суть этой книги:\n' + message_text(update.message)
                elif flag == 2:
                    prompt = 'Найди книгу или книги с похожим описанием и дай их краткое описание\n' + message_text(update.message)
                elif flag == 4:
                    if user_id in self.cooldown:
                        await update.effective_message.reply_text(
                            message_thread_id=get_thread_id(update),
                            text=localized_text('cool_down', self.config['bot_language'])
                            )
                        return
                    else:
                        self.cooldown.append(user_id)
                    prompt = message_text(update.message)
                    prompt = 'Напиши правильное название книги:' + prompt + 'Свой ответ напиши ввиде: Название книги:"..." Автор:...'
                    chat_id = update.effective_chat.id
                    user_name = update.message.from_user.name
                    model_conf = 1
                    stream_response = self.openai.get_chat_response_stream(chat_id=chat_id, query=prompt, model_conf = model_conf)
                    async for content, tokens in stream_response:
                        if tokens == 'not_finished':
                            continue
                        else:
                            remark_prompt = content
                            # Используем регулярные выражения для поиска названия книги и имени автора
                            book_title_match = re.search(r'Название книги:\s+"([^"]+)"', remark_prompt)
                            author_name_match = re.search(r'Автор:\s+([^\.]+)', remark_prompt)

                            # Извлекаем найденные значения и сохраняем их в переменных
                            book_title = book_title_match.group(1) if book_title_match else None
                            author_name = author_name_match.group(1) if author_name_match else None

                            file_path = "/LibAI/pycharmProject/bot/book_info.xlsx"
                            df = pd.read_excel(file_path)
                            if not df.empty:

                                duplicate_rows = df[(df['Имя человека'] == user_name) & (df['Название книги'] == book_title) & (df['Имя автора'] == author_name)]

                                if not duplicate_rows.empty:
                                    # Если есть совпадение, удаляем строку
                                    df = df.drop(duplicate_rows.index)

                                # Проверяем, есть ли уже книги с таким же автором и названием
                                duplicates = df[(df['Название книги'] == book_title) & (df['Имя автора'] == author_name)]

                                if not duplicates.empty:
                                # Если есть совпадения, выводим имена людей, которые вводили эти книги
                                    if not await is_allowed_prem(self.config, update, context):

                                        await update.effective_message.reply_text(
                                        message_thread_id=get_thread_id(update),
                                        text= 'Совпадение найдено!\nПриобретите /premium, чтобы узнать имя пользователя 👀')
                                        
                                    
                                    else:
                                        previous_names = duplicates['Имя человека'].tolist()
                                        previous_id = duplicates['id человека'].tolist()
                                        await update.effective_message.reply_text(
                                            message_thread_id=get_thread_id(update),
                                            text=localized_text('yes_user', self.config['bot_language'])
                                            )
                                        for name in previous_names:
                                            sent_message = await update.effective_message.reply_text(
                                                message_thread_id=get_thread_id(update),
                                                reply_to_message_id=get_reply_to_message_id(self.config, update),
                                                text=name,
                                                )
                                        for id1 in previous_id:  
                                            mes = user_name + " - этот пользователь ищет собеседника для обсуждения книги, которую вы раньше вводили." + "\nКнига - " + book_title + "."
                                            await context.bot.send_message(id1,mes)
                                else:
                                    await update.effective_message.reply_text(
                                        message_thread_id=get_thread_id(update),
                                        text=localized_text('no_user', self.config['bot_language'])
                                        )
                                # Создаем новую строку с информацией
                                new_row = {'Имя человека': user_name,
                                            'Название книги': book_title,
                                            'Имя автора': author_name,
                                            'id человека': chat_id}

                                data_to_append = [new_row]

                                # Преобразуем список словарей в DataFrame и объединяем его с существующим DataFrame
                                new_df = pd.DataFrame(data_to_append)
                                df = pd.concat([df, new_df], ignore_index=True)

                                # Сохраняем обновленный DataFrame в Excel-файл
                                df.to_excel(file_path, index=False)
                                self.cooldown.remove(user_id)
                                return
                            else:
                                # Создаем новую строку с информацией
                                new_row = {'Имя человека': user_name,
                                            'Название книги': book_title,
                                            'Имя автора': author_name,
                                            'id человека': chat_id}

                                data_to_append = [new_row]

                                # Преобразуем список словарей в DataFrame и объединяем его с существующим DataFrame
                                new_df = pd.DataFrame(data_to_append)
                                df = pd.concat([df, new_df], ignore_index=True)

                                # Сохраняем обновленный DataFrame в Excel-файл
                                df.to_excel(file_path, index=False)
                                self.cooldown.remove(user_id)
                                return

                else:
                    prompt = message_text(update.message)
        else:
            await update.effective_message.reply_text(
                message_thread_id=get_thread_id(update),
                text=localized_text('command_no_prompt', self.config['bot_language'])
            )
            return
        self.flag = 0
        self.last_message[chat_id] = prompt

        if user_id in self.cooldown:
            await update.effective_message.reply_text(
                message_thread_id=get_thread_id(update),
                text=localized_text('cool_down', self.config['bot_language'])
            )
            return
        else:
             self.cooldown.append(user_id)

        

        try:
            total_tokens = 0

            if self.config['stream']:
                await update.effective_message.reply_chat_action(
                    action=constants.ChatAction.TYPING,
                    message_thread_id=get_thread_id(update)
                )
                
                if not await is_allowed_prem(self.config, update, context):
                    
                    model_conf = 1
                    
                else:
                    
                    user_name = update.message.from_user.name
                    max_requests_per_day = 10
                    if not await self.limit(user_name, max_requests_per_day):
                        
                            model_conf = 1
                            
                    else:
                        model_conf = 2
                        
                stream_response = self.openai.get_chat_response_stream(chat_id=chat_id, query=prompt, model_conf = model_conf)
                logging.warning(prompt)
                i = 0
                prev = ''
                sent_message = None
                backoff = 0
                stream_chunk = 0

                async for content, tokens in stream_response:
                    if is_direct_result(content):
                        return await handle_direct_result(self.config, update, content)

                    if len(content.strip()) == 0:
                        continue

                    stream_chunks = split_into_chunks(content)
                    if len(stream_chunks) > 1:
                        content = stream_chunks[-1]
                        if stream_chunk != len(stream_chunks) - 1:
                            stream_chunk += 1
                            try:
                                await edit_message_with_retry(context, chat_id, str(sent_message.message_id),
                                                              stream_chunks[-2])
                            except:
                                pass
                            try:
                                sent_message = await update.effective_message.reply_text(
                                    message_thread_id=get_thread_id(update),
                                    text=content if len(content) > 0 else "..."
                                )
                            except:
                                pass
                            continue

                    cutoff = get_stream_cutoff_values(update, content)
                    cutoff += backoff
                    wait = "\nПримерное время ожидания 5-60 секунд."

                    if i == 0:
                        try:
                            if sent_message is not None:
                                await context.bot.delete_message(chat_id=sent_message.chat_id,
                                                                 message_id=sent_message.message_id)
                            sent_message = await update.effective_message.reply_text(
                                message_thread_id=get_thread_id(update),
                                reply_to_message_id=get_reply_to_message_id(self.config, update),
                                text="Пожалуйста, подождите!\nЯ ищу информацию и читаю нужные вам книжки 🧑🏽‍🏫" + wait,
                            )
                        except:
                            continue

                    elif abs(len(content) - len(prev)) > cutoff or tokens != 'not_finished':
                        prev = content

                        try:
                            use_markdown = tokens != 'not_finished'
                            await edit_message_with_retry(context, chat_id, str(sent_message.message_id),
                                                          text=content, markdown=use_markdown)

                        except RetryAfter as e:
                            backoff += 5
                            await asyncio.sleep(e.retry_after)
                            continue

                        except TimedOut:
                            backoff += 5
                            await asyncio.sleep(0.5)
                            continue

                        except Exception:
                            backoff += 5
                            continue

                        await asyncio.sleep(0.01)

                    i += 1
                    if tokens != 'not_finished':
                        total_tokens = int(tokens)

            else:
                async def _reply():
                    nonlocal total_tokens
                    response, total_tokens = await self.openai.get_chat_response(chat_id=chat_id, query=prompt)

                    if is_direct_result(response):
                        return await handle_direct_result(self.config, update, response)

                    # Split into chunks of 4096 characters (Telegram's message limit)
                    chunks = split_into_chunks(response)

                    for index, chunk in enumerate(chunks):
                        try:
                            await update.effective_message.reply_text(
                                message_thread_id=get_thread_id(update),
                                reply_to_message_id=get_reply_to_message_id(self.config,
                                                                            update) if index == 0 else None,
                                text=chunk,
                                parse_mode=constants.ParseMode.MARKDOWN
                            )
                        except Exception:
                            try:
                                await update.effective_message.reply_text(
                                    message_thread_id=get_thread_id(update),
                                    reply_to_message_id=get_reply_to_message_id(self.config,
                                                                                update) if index == 0 else None,
                                    text=chunk
                                )
                            except Exception as exception:
                                raise exception

                await wrap_with_indicator(update, context, _reply, constants.ChatAction.TYPING)

            add_chat_request_to_usage_tracker(self.usage, self.config, user_id, total_tokens)

        except Exception as e:
            logging.exception(e)
            await update.effective_message.reply_text(
                message_thread_id=get_thread_id(update),
                reply_to_message_id=get_reply_to_message_id(self.config, update),
                text=f"{localized_text('chat_fail', self.config['bot_language'])} {str(e)}",
                parse_mode=constants.ParseMode.MARKDOWN
            )
        self.cooldown.remove(user_id)

    async def inline_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Handle the inline query. This is run when you type: @botusername <query>
        """
        query = update.inline_query.query
        if len(query) < 3:
            return
        if not await self.check_allowed_and_within_budget(update, context, is_inline=True):
            return

        callback_data_suffix = "gpt:"
        result_id = str(uuid4())
        self.inline_queries_cache[result_id] = query
        callback_data = f'{callback_data_suffix}{result_id}'

        await self.send_inline_query_result(update, result_id, message_content=query, callback_data=callback_data)

    async def send_inline_query_result(self, update: Update, result_id, message_content, callback_data=""):
        """
        Send inline query result
        """
        try:
            reply_markup = None
            bot_language = self.config['bot_language']
            if callback_data:
                reply_markup = InlineKeyboardMarkup([[
                    InlineKeyboardButton(text=f'🤖 {localized_text("answer_with_chatgpt", bot_language)}',
                                         callback_data=callback_data)
                ]])

            inline_query_result = InlineQueryResultArticle(
                id=result_id,
                title=localized_text("ask_chatgpt", bot_language),
                input_message_content=InputTextMessageContent(message_content),
                description=message_content,
                thumb_url='https://user-images.githubusercontent.com/11541888/223106202-7576ff11-2c8e-408d-94ea'
                          '-b02a7a32149a.png',
                reply_markup=reply_markup
            )

            await update.inline_query.answer([inline_query_result], cache_time=0)
        except Exception as e:
            logging.error(f'An error occurred while generating the result card for inline query {e}')

    async def handle_callback_inline_query(self, update: Update, context: CallbackContext):
        """
        Handle the callback query from the inline query result
        """
        callback_data = update.callback_query.data
        user_id = update.callback_query.from_user.id
        inline_message_id = update.callback_query.inline_message_id
        name = update.callback_query.from_user.name
        callback_data_suffix = "gpt:"
        query = ""
        bot_language = self.config['bot_language']
        answer_tr = localized_text("answer", bot_language)
        loading_tr = localized_text("loading", bot_language)

        try:
            if callback_data.startswith(callback_data_suffix):
                unique_id = callback_data.split(':')[1]
                total_tokens = 0

                # Retrieve the prompt from the cache
                query = self.inline_queries_cache.get(unique_id)
                if query:
                    self.inline_queries_cache.pop(unique_id)
                else:
                    error_message = (
                        f'{localized_text("error", bot_language)}. '
                        f'{localized_text("try_again", bot_language)}'
                    )
                    await edit_message_with_retry(context, chat_id=None, message_id=inline_message_id,
                                                  text=f'{query}\n\n_{answer_tr}:_\n{error_message}',
                                                  is_inline=True)
                    return

                unavailable_message = localized_text("function_unavailable_in_inline_mode", bot_language)
                if self.config['stream']:
                    stream_response = self.openai.get_chat_response_stream(chat_id=user_id, query=query)
                    i = 0
                    prev = ''
                    backoff = 0
                    async for content, tokens in stream_response:
                        if is_direct_result(content):
                            cleanup_intermediate_files(content)
                            await edit_message_with_retry(context, chat_id=None,
                                                          message_id=inline_message_id,
                                                          text=f'{query}\n\n_{answer_tr}:_\n{unavailable_message}',
                                                          is_inline=True)
                            return

                        if len(content.strip()) == 0:
                            continue

                        cutoff = get_stream_cutoff_values(update, content)
                        cutoff += backoff

                        if i == 0:
                            try:
                                await edit_message_with_retry(context, chat_id=None,
                                                              message_id=inline_message_id,
                                                              text=f'{query}\n\n{answer_tr}:\n{content}',
                                                              is_inline=True)
                            except:
                                continue

                        elif abs(len(content) - len(prev)) > cutoff or tokens != 'not_finished':
                            prev = content
                            try:
                                use_markdown = tokens != 'not_finished'
                                divider = '_' if use_markdown else ''
                                text = f'{query}\n\n{divider}{answer_tr}:{divider}\n{content}'

                                # We only want to send the first 4096 characters. No chunking allowed in inline mode.
                                text = text[:4096]

                                await edit_message_with_retry(context, chat_id=None, message_id=inline_message_id,
                                                              text=text, markdown=use_markdown, is_inline=True)

                            except RetryAfter as e:
                                backoff += 5
                                await asyncio.sleep(e.retry_after)
                                continue
                            except TimedOut:
                                backoff += 5
                                await asyncio.sleep(0.5)
                                continue
                            except Exception:
                                backoff += 5
                                continue

                            await asyncio.sleep(0.01)

                        i += 1
                        if tokens != 'not_finished':
                            total_tokens = int(tokens)

                else:
                    async def _send_inline_query_response():
                        nonlocal total_tokens
                        # Edit the current message to indicate that the answer is being processed
                        await context.bot.edit_message_text(inline_message_id=inline_message_id,
                                                            text=f'{query}\n\n_{answer_tr}:_\n{loading_tr}',
                                                            parse_mode=constants.ParseMode.MARKDOWN)

                        logging.info(f'Generating response for inline query by {name}')
                        response, total_tokens = await self.openai.get_chat_response(chat_id=user_id, query=query)

                        if is_direct_result(response):
                            cleanup_intermediate_files(response)
                            await edit_message_with_retry(context, chat_id=None,
                                                          message_id=inline_message_id,
                                                          text=f'{query}\n\n_{answer_tr}:_\n{unavailable_message}',
                                                          is_inline=True)
                            return

                        text_content = f'{query}\n\n_{answer_tr}:_\n{response}'

                        # We only want to send the first 4096 characters. No chunking allowed in inline mode.
                        text_content = text_content[:4096]

                        # Edit the original message with the generated content
                        await edit_message_with_retry(context, chat_id=None, message_id=inline_message_id,
                                                      text=text_content, is_inline=True)

                    await wrap_with_indicator(update, context, _send_inline_query_response,
                                              constants.ChatAction.TYPING, is_inline=True)

                add_chat_request_to_usage_tracker(self.usage, self.config, user_id, total_tokens)

        except Exception as e:
            logging.error(f'Failed to respond to an inline query via button callback: {e}')
            logging.exception(e)
            localized_answer = localized_text('chat_fail', self.config['bot_language'])
            await edit_message_with_retry(context, chat_id=None, message_id=inline_message_id,
                                          text=f"{query}\n\n_{answer_tr}:_\n{localized_answer} {str(e)}",
                                          is_inline=True)

    

    async def check_allowed_and_within_budget(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                                              is_inline=False) -> bool:

        name = update.inline_query.from_user.name if is_inline else update.message.from_user.name
        user_id = update.inline_query.from_user.id if is_inline else update.message.from_user.id

        if not await is_allowed(self.config, update, context, is_inline=is_inline):
            logging.warning(f'User {name} (id: {user_id}) is not allowed to use the bot')
            await self.send_disallowed_message(update, context, is_inline)
            return False
        if not is_within_budget(self.config, self.usage, update, is_inline=is_inline):
            logging.warning(f'User {name} (id: {user_id}) reached their usage limit')
            await self.send_budget_reached_message(update, context, is_inline)
            return False

        return True

    async def send_disallowed_message(self, update: Update, _: ContextTypes.DEFAULT_TYPE, is_inline=False):
        """
        Sends the disallowed message to the user.
        """
        if not is_inline:
            await update.effective_message.reply_text(
                message_thread_id=get_thread_id(update),
                text=self.disallowed_message,
                disable_web_page_preview=True
            )
        else:
            result_id = str(uuid4())
            await self.send_inline_query_result(update, result_id, message_content=self.disallowed_message)

    async def send_budget_reached_message(self, update: Update, _: ContextTypes.DEFAULT_TYPE, is_inline=False):
        """
        Sends the budget reached message to the user.
        """
        if not is_inline:
            await update.effective_message.reply_text(
                message_thread_id=get_thread_id(update),
                text=self.budget_limit_message
            )
        else:
            result_id = str(uuid4())
            await self.send_inline_query_result(update, result_id, message_content=self.budget_limit_message)

    async def post_init(self, application: Application) -> None:
        """
        Post initialization hook for the bot.
        """
        await application.bot.set_my_commands(self.group_commands, scope=BotCommandScopeAllGroupChats())
        await application.bot.set_my_commands(self.commands)

    
        
            
                           
    def run(self):
        """
        Runs the bot indefinitely until the user presses Ctrl+C
        """
        application = ApplicationBuilder() \
            .token(self.config['token']) \
            .proxy_url(self.config['proxy']) \
            .get_updates_proxy_url(self.config['proxy']) \
            .post_init(self.post_init) \
            .concurrent_updates(True) \
            .build()
        
        application.add_handler(CommandHandler('addPrem', self.addPrem))
        application.add_handler(CommandHandler('premium', self.premium))
        application.add_handler(CommandHandler('buy', self.buy))
        application.add_handler(CommandHandler('reset', self.reset))
        application.add_handler(CommandHandler('help', self.help))
        application.add_handler(CommandHandler('start', self.help))
        application.add_handler(CommandHandler('booksearch', self.booksearch))
        application.add_handler(CommandHandler('bookretell', self.bookretell))
        application.add_handler(CommandHandler('bookmatch', self.bookmatch))
        application.add_handler(CommandHandler('booktalk', self.gpt))
        application.add_handler(CommandHandler(
            'chat', self.prompt, filters=filters.ChatType.GROUP | filters.ChatType.SUPERGROUP)
        )
        
        application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), self.prompt))
        application.add_handler(InlineQueryHandler(self.inline_query, chat_types=[
            constants.ChatType.GROUP, constants.ChatType.SUPERGROUP, constants.ChatType.PRIVATE
        ]))
        application.add_handler(CallbackQueryHandler(self.handle_callback_inline_query))
        application.add_error_handler(error_handler)
        
        
        
            

        
        application.run_polling()
        
