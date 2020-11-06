import asyncio
import os
import re
import logging

import discord
from discord import Client, File
from discord.ext import commands
from fuzzywuzzy.process import extractOne as fuzzy_select
from google.protobuf.json_format import MessageToDict

from voice import DemultiplexerSink, Audio
from utils import registry, extract_intent

logger = logging.getLogger(__name__)
token = os.getenv('DISCORD_BOT_TOKEN')


def remove_mentions(text):
    text = re.sub(r'<@(everyone|here|[!&]?[0-9]{17,21})>', '', text)
    text = text.strip()
    return text


class OrginizerCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author == self.bot.user:
            return
        # Do not process messages without bot mention
        # to optimize amount of queries to Dialogflow
        for mention in message.mentions:
            if mention == self.bot.user:
                break
        else:
            return
        content = remove_mentions(message.content)
        response = extract_intent(message.author.id, content)
        action = response.query_result.action
        parameters = MessageToDict(response.query_result.parameters)
        print(f'action: {action}, parameters: {parameters}')
        if action == 'play':
            song = parameters['any']
            await self.play(ctx=message, query=song)
        elif action == 'connect':
            await self.connect(message)
        elif action == 'move':
            name = parameters['person']['name']
            room = parameters['any']
            query = f'{name} to {room}'
            await self.move(ctx=message, query=query)
        elif action == 'call':
            name = parameters['any']
            room = message.author.voice.channel.name
            query = f'{name} to {room}'
            # TODO: do not move users without accepts
            await self.move(ctx=message, query=query)
        else:
            text_response = response.query_result.fulfillment_messages[0].text.text[0]
            await message.channel.send(text_response)

    @commands.Cog.listener()
    async def on_command_error(self, ctx, err):
        # To prevent discord.ext.commands.errors.CommandNotFound errors
        # caused by running of on_message instead of @commands.command()
        if isinstance(err, commands.errors.CommandNotFound):
            pass

    @commands.command()
    async def connect(self, ctx):
        if ctx.author.voice is None:
            await ctx.channel.send('I can\'t connect. You are not in a voice channel.')
            return
        voice_channel = ctx.author.voice.channel
        await voice_channel.connect()

    @commands.command()
    async def members(self, ctx):
        guild = ctx.guild
        for member in guild.members:
            print(repr(member))

    @commands.command()
    # *, query concatenates all the params in a single string
    async def move(self, ctx, *, query):
        guild = ctx.guild
        print('query:', query)

        name = query.split('to')[0]
        name = name.replace('"', '')
        name = name.strip()
        # TODO: Fuzzy matching should be implemented on the Dialogflow side
        candidates = [m.name for m in guild.members] + [m.nick for m in guild.members if m.nick is not None]
        fuzzy_name, name_similarity = fuzzy_select(name, candidates)
        member = None
        for m in guild.members:
            if m.nick == fuzzy_name or m.name == fuzzy_name:
                member = m
        print('fuzzy_query:', name, 'result:', repr(member), 'similarity:', name_similarity)

        channel_name = query.split('to')[1]
        channel_name = channel_name.replace('"', '')
        channel_name = channel_name.strip()
        candidates = [c.name for c in guild.voice_channels]
        fuzzy_name, channel_similarity = fuzzy_select(channel_name, candidates)
        channel = None
        for c in guild.voice_channels:
            if c.name == fuzzy_name:
                channel = c
        print('fuzzy_query:', channel_name, 'result:', repr(channel), 'similarity:', channel_similarity)
        await member.move_to(channel)

    @commands.command()
    async def play(self, ctx, *, query):
        """Plays a file from the local filesystem"""
        source = discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(query))
        try:
            ctx.voice_client.play(source, after=lambda e: print('Player error: %s' % e) if e else None)
            await ctx.send('Now playing: {}'.format(query))
        except AttributeError:
            ctx.guild.voice_client.play(source, after=lambda e: print('Player error: %s' % e) if e else None)
            await ctx.channel.send('Now playing: {}'.format(query))


class OrginizerBot(commands.bot.BotBase, Client):
    def __init__(self):
        # discord_intents = discord.Intents.default()
        # discord_intents.members = True
        super().__init__(
            command_prefix=commands.when_mentioned_or('!'),
            description='Orginizer bot that can talk with group of people',
            # intents=discord_intents,
        )
        discord.opus.load_opus('/usr/local/Cellar/opus/1.3.1/lib/libopus.0.dylib')
        print('OPUS:', discord.opus.is_loaded())
        self.voice_bots = dict()

    async def on_ready(self):
        logger.info(f"Logged in as {self.user}")
        channels = {channel.name: channel for channel in self.get_all_channels()}
        handler = DiscordHandler(channels['boss-only'])
        handler.setLevel(logging.INFO)
        formatter = logging.Formatter('%(name)s: %(message)s')
        handler.setFormatter(formatter)
        for logger_name in ['discord', 'cities', 'akinator', 'googlesearch', 'parlai', 'youtubedl']:
            logging.getLogger(logger_name).addHandler(handler)
        logger.info(f"Guilds: {self.guilds}")
        for guild in self.guilds:
            logger.info(f"{guild.name} channels: {guild.channels}")
            logger.info(f"{guild.name} voice channels: {guild.voice_channels}")
            voice_channel = discord.utils.get(guild.voice_channels)
            voice_client = await voice_channel.connect()
            voice_bot = DemultiplexerSink(voice_client, [
                'bumblebee',
            ])
            await voice_bot.start()
            self.voice_bots[voice_channel] = voice_bot

    async def on_guild_join(self, guild):
        logger.debug(f"{self!r} joined {guild}")

    async def on_group_join(self, channel, user):
        logger.debug(f"{self!r} observed that {user} joined {channel}")

    async def on_voice_state_update(self, user, old_state, new_state):
        """ Вызывается, когда пользователь заходит на канал, включает / отключает звук или микрофон """
        if user != self.user and old_state.channel is None and new_state.channel is not None:
            await self.voice_bots[new_state.channel].on_welcome(user)


class DialogflowCog(commands.Cog):
    @commands.command()
    async def join(self, ctx):
        """Joins a voice channel"""

        if ctx.voice_client.channel != ctx.author.voice.channel:
            return await ctx.voice_client.move_to(ctx.author.voice.channel)

    @commands.command()
    async def stop(self, ctx):
        """Stops and disconnects the bot from voice"""

        await ctx.voice_client.disconnect()

    @commands.command()
    async def youtube_dl(self, ctx, url: str):
        # TODO: how to get voice channel from text channel?
        skill_ctx = ctx.bot.voice_bots[ctx.channel].users[ctx.author]
        await registry.run_skill('youtube-dl', skill_ctx, ctx.author, url)

    @commands.command()
    async def parlai(self, ctx, model: str = 'reddit-2020-07-01'):
        await registry.run_skill('parlai', )

    @join.before_invoke
    async def ensure_voice(self, ctx):
        if ctx.voice_client is None:
            if ctx.author.voice:
                logger.debug(f'Joining channel {ctx.author.voice.channel!r}')
                await ctx.author.voice.channel.connect()
            else:
                await ctx.send("You are not connected to a voice channel.")
                raise commands.CommandError("Author not connected to a voice channel.")
        elif ctx.voice_client.is_playing():
            ctx.voice_client.stop()


class DiscordHandler(logging.Handler):
    def __init__(self, channel):
        super().__init__()
        self.channel = channel
        self.loop = asyncio.get_running_loop()

    async def send_message(self, speech: Audio, message: str):
        await self.channel.send(f'```{message}```', file=speech and File(fp=speech.to_wav(), filename='speech.wav'))

    def emit(self, record: logging.LogRecord):
        asyncio.run_coroutine_threadsafe(
            self.send_message(getattr(record, 'speech', None), self.format(record)), self.loop)


def setup_logging():
    logging.basicConfig(level=logging.INFO, format='%(asctime)-15s %(levelname)s[%(name)-20s] %(message)s')
    logging.getLogger('discord.gateway').setLevel(logging.WARNING)
    for level, name in logging._levelToName.items():
        for logger_name in os.getenv(f'LOGGERS_{name}', '').split(','):
            if logger_name:
                logging.getLogger(logger_name).setLevel(level)


def main():
    # By default the information about the guild members is not available
    # https://discordpy.readthedocs.io/en/latest/intents.html#where-d-my-members-go
    # discord_intents = discord.Intents.default()
    # discord_intents.members = True
    setup_logging()
    logger.info(f"Loaded skills: {list(registry.skills)}")
    bot = OrginizerBot()
    bot.add_cog(OrginizerCog(bot))
    bot.run(token)


if __name__ == '__main__':
    main()
