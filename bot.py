import os

import discord
from discord.ext import commands
from fuzzywuzzy.process import extractOne as fuzzy_select


token = os.environ.get('DISCORD_BOT_TOKEN')


class OrginizerCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

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
        ctx.voice_client.play(source, after=lambda e: print('Player error: %s' % e) if e else None)
        await ctx.send('Now playing: {}'.format(query))


def main():
    # https://discordpy.readthedocs.io/en/latest/intents.html#where-d-my-members-go
    discord_intents = discord.Intents.default()
    discord_intents.members = True
    bot = commands.Bot(command_prefix=commands.when_mentioned_or('!'), intents=discord_intents)

    @bot.event
    async def on_ready():
        print('We have logged in as {0.user}'.format(bot))

    bot.add_cog(OrginizerCog(bot))
    bot.run(token)


if __name__ == '__main__':
    main()
