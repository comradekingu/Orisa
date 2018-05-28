# Orisa, a simple Discord bot with good intentions 
# Copyright (C) 2018 Dennis Brakhane
# 
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, version 3 only
# 
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
# 
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
import logging
import logging.config

import re
import random
from bisect import bisect
from datetime import datetime
from itertools import groupby
from operator import itemgetter

import asks
import curio
import html5lib
import multio
import yaml
from curious.commands.context import Context
from curious.commands.decorators import command, condition
from curious.commands.exc import ConversionFailedError
from curious.commands.manager import CommandsManager
from curious.commands.plugin import Plugin
from curious.core.client import Client
from curious.exc import HierarchyError
from curious.dataclasses.embed import Embed
from curious.dataclasses.member import Member
from curious.dataclasses.presence import Game, Status
from fuzzywuzzy import process
from lxml import html


from config import BOT_TOKEN, GUILD_ID, CHANNEL_ID, CONGRATS_CHANNEL_ID, OWNER_ID
from models import Database, User

with open('logging.yaml') as logfile:
    logging.config.dictConfig(yaml.safe_load(logfile))

logger = logging.getLogger("orisa")

class InvalidBattleTag(Exception):
    def __init__(self, message):
        self.message = message

class UnableToFindSR(Exception):
    pass

class NicknameTooLong(Exception):
    def __init__(self, nickname):
        self.nickname = nickname

RANK_CUTOFF = (1500, 2000, 2500, 3000, 3500, 4000)
RANKS = ('Bronze', 'Silver', 'Gold', 'Platinum', 'Diamond', 'Master', 'Grand Master')
COLORS = (
    0xcd7e32, # Bronze
    0xc0c0c0, # Silver
    0xffd700, # Gold
    0xe5e4e2, # Platinum
    0xa2bfd3, # Diamond
    0xf9ca61, # Master
    0xf1d592, # Grand Master
)


def get_rank(sr):
    return bisect(RANK_CUTOFF, sr) if sr is not None else None

async def get_sr_rank(battletag):
    if not re.match(r'\w+#[0-9]+', battletag):
        raise InvalidBattleTag('Malformed BattleTag. BattleTags look like SomeName#1234: a name and a # sign followed by a number and contain no spaces. They are case-sensitive, too!')

    url = f'https://playoverwatch.com/en-us/career/pc/{battletag.replace("#", "-")}'
    logger.info(f'requesting {url}')
    result = await asks.get(url)
    if result.status_code != 200:
        raise RuntimeError(f'got status code {result.status_code} from Blizz')

    document = html.fromstring(result.content)
    srs = document.xpath('//div[@class="competitive-rank"]/div/text()')
    rank_image_elems = document.xpath('//div[@class="competitive-rank"]/img/@src')
    if not srs:
        if 'Profile Not Found' in result.text:
            raise InvalidBattleTag(f"No profile with BattleTag {battletag} found. BattleTags are case-sensitive!")
        raise UnableToFindSR()
    sr = int(srs[0])
    if rank_image_elems:
        rank_image = str(rank_image_elems[0])
    else:
        rank_image = None
    return (sr, get_rank(sr), rank_image)


def correct_guild(ctx):
    return ctx.guild.id == GUILD_ID

def correct_channel(ctx):
    return ctx.channel.id == CHANNEL_ID or ctx.channel.private

def only_owner(ctx):
    return ctx.author.id == OWNER_ID and ctx.channel.private


async def send_long(send_func, msg):
    "Splits a long message >2000 into smaller chunks"

    if len(msg) <= 2000:
        await send_func(msg)
        return
    else:
        lines = msg.split("\n")

        part = ""
        for line in lines:
            if len(part) + len(line) > 2000:
                await send_func(part)
                part = ""
            part += line + "\n"

        if part:
            await send_func(part)

class Orisa(Plugin):

    def __init__(self, client, database):
        super().__init__(client)
        self.database = database

    async def load(self):
        await self.spawn(self._sync_all_users_task)


    # admin commands

    @command()
    @condition(only_owner)
    async def shutdown(self, ctx):
        logger.critical("GOT EMERGENCY SHUTDOWN COMMAND FROM OWNER")
        await self.client.kill()
        raise SystemExit(42)

    @command()
    @condition(only_owner)
    async def messageall(self, ctx, *, message: str):
        s = self.database.Session()
        try:
            users = s.query(User).all()
            for user in users:
                try:
                    logger.debug(f"Sending message to {user.discord_id}")
                    chan = await self.client.guilds[GUILD_ID].members[user.discord_id].user.open_private_channel()
                    await chan.messages.send(message)
                except:
                    logger.exception(f"Error while sending to {user.discord_id}")
            logger.debug("Done sending")
        finally:
            s.close()

    @command()
    @condition(only_owner)
    async def post(self, ctx, channel_id: int, *, message:str):
        channel = self.client.find_channel(channel_id)
        msg = await channel.messages.send(message)
        await ctx.channel.messages.send(f"created {msg.id}")

    @command()
    @condition(only_owner)
    async def delete(self, ctx, channel_id: int, message_id: int):
        # low level access, because getting a message requires MESSAGE_HISTORY permission
        await self.client.http.delete_message(channel_id, message_id)
        await ctx.channel.messages.send("deleted")

    @command()
    @condition(only_owner)
    async def updatehelp(self, ctx, channel_id: int, message_id: int):
        await self.client.http.edit_message(channel_id, message_id, embed=self._create_help().to_dict())
        await ctx.channel.messages.send("done")


    # bt commands

    @command()
    @condition(correct_channel)
    async def bt(self, ctx, *, member: Member = None):

        member_given = member is not None
        if not member_given:
            member = ctx.author

        session = self.database.Session()

        content = embed = None
        try:
            user = self.database.by_discord_id(session, member.id)
            if user:
                embed = Embed(colour=0x659dbd) # will be overwritten later if SR is set
                embed.add_field(name="Nick", value=member.name)
                embed.add_field(name="BattleTag", value=f"**{user.battle_tag}**")
                if user.sr:
                    rank = get_rank(user.sr)
                    embed.add_field(name="SR", value=f"{user.sr} ({RANKS[rank]})")
                    embed.colour = COLORS[rank]
                if member == ctx.author and member_given:
                    embed.set_footer(text="BTW, you do not need to specify your nickname if you want your own BattleTag; just !bt is enough")
            else:
                content = f"{member.name} not found in database! *Do you need a hug?*"
                if member == ctx.author:
                    embed = Embed(
                                title="Hint",
                                description="use `!bt register BattleTag#1234` to register, or `!bt help` for more info"
                            )
        finally:
            session.close()

        await ctx.channel.messages.send(content=content, embed=embed)

    @bt.subcommand()
    @condition(correct_channel)
    async def get(self, ctx, *, member: Member = None):
        r = await self.bt(ctx, member=member)
        return r

    @bt.subcommand()
    @condition(correct_channel)
    async def register(self, ctx, battle_tag: str = None):
        if battle_tag is None:
            await ctx.channel.messages.send(f"{ctx.author.mention} missing BattleTag")
            return
        member_id = ctx.message.author_id
        session = self.database.Session()
        try:
            user = self.database.by_discord_id(session, member_id)
            resp = None
            if user is None:
                user = User(discord_id=member_id)
                session.add(user)
                resp = ("OK. People can now ask me for your BattleTag, and I will update your nick whenever I notice that your SR changed.\n"
                        "If you want, you can also join the Overwatch role by typing `.iam Overwatch` (mind the leading dot) in the overwatch-stats "
                        "channel, this way, you can get notified by shoutouts to @Overwatch\n")
            else:
                logger.info(f"{ctx.author.id} requested to change his BattleTag from {user.battle_tag} to {battle_tag}")
                if user.battle_tag == battle_tag:
                    await ctx.channel.messages.send(f"{ctx.author.mention} You already registered with that same BattleTag, so there's nothing for me to do. *Sleep mode reactivated.*")
                    return
                resp = "OK. I've updated your BattleTag."
            await ctx.channel.send_typing() # show that we're working
            try:
                sr, rank, image = await get_sr_rank(battle_tag)
            except InvalidBattleTag as e:
                await ctx.channel.messages.send(f"{ctx.author.mention} Invalid BattleTag: {e.message}")
                raise
            except UnableToFindSR:
                resp += "\nYou don't have an SR though, you probably need to finish your placement matches... I still saved your BattleTag."
                sr = None

            user.battle_tag = battle_tag
            user.last_update = datetime.now()
            user.sr = sr
            user.format = "%s"
            user.highest_rank = get_rank(sr) 

            try:
                await self._update_nick(user)
            except NicknameTooLong as e:
                resp += (f"\n**Adding your SR to your nickname would result in '{e.nickname}' and with {len(e.nickname)} characters, be longer than Discord's maximum of 32.** Please shorten your nick to be no longer than 28 characters. I will regularly try to update it.") 

            except Exception as e:
                logger.exception(f"unable to update nick for user {user}")
                resp += ("\nHowever, right now I couldn't update your nickname, will try that again later. If you are a clan admin, "
                         "I simply cannot update your nickname ever, period. People will still be able to ask for your BattleTag, though.")
        finally: 
            session.commit() # we always want to commit, because we have error_count
            session.close()
        
        await ctx.channel.messages.send(f"{ctx.author.mention} {resp}")

    @bt.subcommand()
    @condition(correct_channel)
    async def format(self, ctx, *, format: str):
        if ']' in format:
            await ctx.channel.messages.send(f"{ctx.author.mention}: format string may not contain square brackets")
            return
        if ('%s' not in format) and ('%r' not in format):
            await ctx.channel.messages.send(f"{ctx.author.mention}: format string must contain at least a %s or %r")
            return
        if not format:
            await ctx.channel.messages.send(f"{ctx.author.mention}: format string missing")
            return
        
        session = self.database.Session()
        
        try:
            user = self.database.by_discord_id(session, ctx.author.id)
            if not user:
                await ctx.channel.messages.send(f"{ctx.author.mention}: you must register first")
                return
            else:
                user.format = format
                try:
                    new_nick = await self._update_nick(user)
                except NicknameTooLong as e:
                    await ctx.channel.messages.send(
                            f"{ctx.author.mention} Sorry, using this format would make your nickname be longer than 32 characters ({len(e.nickname)} to be exact).\n"
                            f"Please choose a shorter format or shorten your nickname")
                    session.rollback()
                else:
                    titles = [
                            "Smarties Expert",
                            "Bread Scientist",
                            "Eternal Bosom of Hot Love",
                            "Sith Lord of Security",
                            "Namer of Clouds",
                            "Scourge of Beer Cans",
                            ]

                    await ctx.channel.messages.send(f'{ctx.author.mention} Done. Henceforth, ye shall be knownst as "{new_nick}, {random.choice(titles)}."')
        finally:
            session.commit()
            session.close()
            
    @bt.subcommand()
    @condition(correct_channel)
    async def forceupdate(self, ctx):
        session = self.database.Session()
        try:
            logger.info(f"{ctx.author.id} used forceupdate")
            user = self.database.by_discord_id(session, ctx.author.id)
            if not user:
                await ctx.channel.messages.send(f"{ctx.author.mention} you are not registered")
            else:
                await ctx.channel.messages.send(f"{ctx.author.mention} OK, I will update your data immediately. If your SR is not up to date, you need to log out of Overwatch once and try again.")
                await self._sync_user(user)
        except Exception as e:
            logger.exception(f'exception while syncing {user}')
        finally:
            session.commit()
            session.close()            
        
    @bt.subcommand()
    async def forgetme(self, ctx):
        session = self.database.Session()
        try:
            user = self.database.by_discord_id(session, ctx.author.id)
            if user:
                logger.info(f"{ctx.author.name} ({ctx.author.id}) requested removal")
                session.delete(user)
                await ctx.channel.messages.send(f"OK, deleted {ctx.author.name} from database")
                session.commit()
            else:
                await ctx.channel.messages.send(f"{ctx.author.mention} you are not registered anyway, so there's nothing for me to forget...")
        finally:
            session.close()

    @bt.subcommand()
    @condition(correct_channel)
    async def findplayers(self, ctx, sr_diff: int = None, base_sr: int = None):
        await self._findplayers(ctx, sr_diff, base_sr, findall=False)

    @bt.subcommand()
    @condition(correct_channel)
    async def findallplayers(self, ctx, sr_diff: int = None, base_sr: int = None):
        await self._findplayers(ctx, sr_diff, base_sr, findall=True)

    async def _findplayers(self, ctx, sr_diff: int = None, base_sr: int = None, *, findall):
        logger.info(f"{ctx.author.id} issued findplayers {sr_diff} {base_sr} {findall}")

        if sr_diff is not None:
            if sr_diff <= 0:
                await ctx.channel.messages.send("SR difference must be positive")
                return

            if sr_diff > 5000:
                await ctx.channel.messages.send("You just had to try ridiculous values, didn't you?")
                return

        session = self.database.Session()
        try:
            asker = self.database.by_discord_id(session, ctx.author.id)
            if not asker:
                await ctx.channel.messages.send(f"{ctx.author.mention} you are not registered")
                return

            if not base_sr:
                base_sr = asker.sr
            if not sr_diff:
                sr_diff = 1000 if asker.sr < 3500 else 500


            if not (500 <= base_sr <= 5000):
                await ctx.channel.messages.send(f"The lowest possible SR is 500, and the highest is 5000, and you say your SR is {base_sr}? *Suuuure.*")
                return

            candidates = session.query(User).filter(User.sr.between(base_sr - sr_diff, base_sr + sr_diff)).all()
    
            cmap = {c.discord_id: c for c in candidates}

            guild = self.client.guilds[GUILD_ID]
            

            online = []
            offline = []

            for member in guild.members.values():
                if member.user.id == ctx.author.id or member.user.id not in cmap:
                    continue
                if member.status == Status.OFFLINE:
                    offline.append(member)
                else:
                    online.append(member)


            def format_member(member):
                nonlocal cmap
                markup = "~~" if member.status == Status.DND else ""

                if member.status == Status.IDLE:
                    hint = "(idle)"
                elif member.status == Status.DND:
                    hint = "(DND)"
                else:
                    hint = ""

                #return f"{str(m.name)} {m.mention} ({cmap[m.user.id].sr})"
                return f"{markup}{str(member.name)}\u00a0{member.mention}{markup}\u00a0{hint}\n"


            msg = ""
           
            if not online:
                msg += f"There are no players currently online within {sr_diff} of {base_sr} SR.\n\n"
            else:
                msg += f"**The following players are currently online and within {sr_diff} SR of {base_sr}:**\n\n"
                msg += "\n".join(format_member(m) for m in sorted(online, key=lambda m:cmap[m.user.id].sr))
                msg += "\n"

            if findall:

                if not offline:
                    if online:
                        msg += "There are no offline players within that range."
                    else:
                        msg += "There are also no offline players within that range. :("
                else:
                    msg += "**The following players are within that range, but currently offline:**\n\n"
                    msg += "\n".join(format_member(m) for m in sorted(offline, key=lambda m:cmap[m.user.id].sr))

            else:
                if offline:
                    msg += f"\nThere are also {len(offline)} offline players within that range. Use the `findallplayers` "
                    msg += "command to show them as well."
        
            await send_long(ctx.author.send, msg)
            if not ctx.channel.private:
                await ctx.channel.messages.send(f"{ctx.author.mention} I sent you a DM with the results.")

        finally:
            session.close()

    @bt.subcommand()
    async def help(self, ctx):
        await ctx.author.send(content=None, embed=self._create_help())
        if not ctx.channel.private:
            await ctx.channel.messages.send(f"{ctx.author.mention} I sent you a DM with instructions.")


    def _create_help(self):
        embed = Embed(
            title="Orisa's purpose",
            description=(
                "When joining a QP or Comp channel, you need to know the BattleTag of a channel member, or they need "
                "yours to add you. In competitive channels it also helps to know which SR the channel members have. "
                "To avoid having to ask for this information again and again when joining a channel, this bot was created. "
                "When you register with your BattleTag, your nick will automatically be updated to show your "
                "current SR and it will be kept up to date. You can also ask for other member's BattleTag, or request "
                "your own so others can easily add you in OW.\n"
                "It will also send a short message to the chat when you ranked up.\n"
                f"*Like Overwatch's Orisa, this bot is quite young and still new at this. Report issues to <@!{OWNER_ID}>*\n"
                f"\n**The commands only work in the <#{CHANNEL_ID}> channel or by sending me a DM**"),
        )
        embed.add_field(
            name='!bt [nick]', 
            value=('Shows the BattleTag for the given nickname, or your BattleTag '
                   'if no nickname is given. `nick` can contain spaces. A fuzzy search for the nickname is performed.\n'
                   '*Examples:*\n'
                   '`!bt` will show your BattleTag\n'
                   '`!bt the chosen one` will show the BattleTag of "tHE ChOSeN ONe"\n'
                   '`!bt orisa` will show the BattleTag of "SG | Orisa", "Orisa", or "Orisad"\n'
                   '`!bt oirsa` and `!bt ori` will probably also show the BattleTag of "Orisa"')
        )
        embed.add_field(
            name='!bt get nick', 
            value=('Same as `!bt [nick]`, (only) useful when the nick is the same as a command.\n'
                   '*Example:*\n'
                   '`!bt get register foo` will search for the nick "register foo"')
        )

        embed.add_field(
            name='!bt register BattleTag#1234', 
            value='Registers or updates your account with the given BattleTag. '
                  'Your OW account will be checked periodically and your nick will be '
                  'automatically updated to show your SR or rank (see the *format* command for more info). '
                  '`register` will fail if the BattleTag is invalid. *BattleTags are case-sensitive!*'
        )
        embed.add_field(
            name='!bt findplayers [max diff] [sr to compare]',
            value='*This command is still in beta and may change at any time!*\n'
                  'This command is intended to find partners for your Competitive team and shows you all registered and online users within the specified range.\n'
                  'If `max diff` is not given, the maximum range that allows you to queue with them is used, so 1000 below 3500 SR, and 500 otherwise. '
                  'If `sr to compare` is not given, your SR is used, otherwise that given SR is compared against. You should only need to specify this when you currently have no rank.\n'
                  '*Examples:*\n'
                  '`!bt findplayers`: finds all players that you could start a competitive queue with\n'
                  '`!bt findplayers 123`: finds all players that are within 123 SR of your SR\n'
                  '`!bt findplayers 1000 2150`: finds all players between 1150 and 3150 SR. To be used when Orisa doesn\'t know your previous SR was around 2150.\n'
        )
        embed.add_field(
            name='!bt findallplayers [max diff] [sr to compare]',
            value='Same as `findplayers`, but also includes offline players'
        )
        embed.add_field(
            name='!bt format *format*',
            value="Lets you specify how your SR or rank is displayed. It will always "
                  "be shown in [square\u00a0brackets] appended to your name. "
                  "In the *format*, `%s` will be replaced with your SR, and `%r` "
                  "will be replaced with your rank.\n"
                  '*Examples:*\n'
                  '`!bt format test %s SR` will result in [test 2345 SR]\n'
                  '`!bt format Potato/%r` in [Potato/Gold].\n'
                  '*Default: `%s`*'
        )
        embed.add_field(
            name='!bt forceupdate', 
            value='Immediately checks your account data and updates your nick accordingly.\n'
                  '*Checks and updates are done automatically, use this command only if '
                  'you want your nick to be up to date immediately!*'
        )
        embed.add_field(
            name='!bt forgetme', 
            value='Your BattleTag will be removed from the database and your nick '
                  'will not be updated anymore. You can re-register at any time.'
        )

        return embed

    def _format_nick(self, format, sr):
        rankno = get_rank(sr)
        rank = RANKS[rankno] if rankno is not None else "Unranked"
        srstr = str(sr) if sr is not None else "noSR"

        return format.replace('%s', srstr).replace('%r', rank)


    async def _update_nick(self, user):
        user_id = user.discord_id

        nn = str(self.client.guilds[GUILD_ID].members[user_id].name)
        formatted = self._format_nick(user.format, user.sr)
        if re.search(r'\[.*?\]', str(nn)):
            new_nn = re.sub(r'\[.*?\]', f'[{formatted}]', nn)
        else:
            new_nn = f'{nn} [{formatted}]'
       
        if len(new_nn) > 32:
            raise NicknameTooLong(new_nn)

        if str(nn) != new_nn:
            await self.client.guilds[GUILD_ID].members[user_id].nickname.set(new_nn)

        return new_nn

    async def _send_congrats(self, user, rank, image):

        embed = Embed(
            title=f"For your own safety, get behind the barrier!",
            description=f"**{str(self.client.guilds[GUILD_ID].members[user.discord_id].name)}** just advanced to **{RANKS[rank]}**. Congratulations!",
            colour=COLORS[rank],
        )

        embed.set_thumbnail(url=image)

        await self.client.find_channel(CONGRATS_CHANNEL_ID).messages.send(embed=embed)

    async def _sync_user(self, user):
        user.last_update = datetime.now()
        try:
            sr, rank, image = await get_sr_rank(user.battle_tag)
        except UnableToFindSR:
            logger.debug(f"No SR for {user.battle_tag}, oh well...")
            # it is successful, after all
            user.error_count = 0
        except Exception:
            user.error_count += 1
            logger.exception(f"Got exception while requesting {user.battle_tag}")
            raise
        else:
            user.error_count = 0
            user.sr = sr
            try:
                await self._update_nick(user)
            except HierarchyError:
                # not much we can do, just ignore
                pass
            except NicknameTooLong as e:
                discord_user = await self.client.get_user(user.discord_id)
                channel = await discord_user.open_private_channel()
                msg = f"Hi! I just tried to update your nickname, but the result '{e.nickname}' would be longer than 32 characters."
                if user.format == "%s":
                    msg += "\nPlease shorten your nickname."
                else:
                    msg += "\nTry to use the %s format (you can type `!bt format %s` into this DM channel, or shorten your nickname."
                msg += "\nYour nickname cannot be updated until this is done. I'm sorry for the inconvenience."
                await channel.messages.send(msg)

                # we can still do the rest, no need to return here
            if rank is not None:
                if user.highest_rank is None:
                    user.highest_rank = rank

                elif rank < user.highest_rank and sr % 500 <= 350:
                    # user has fallen at least 150 below threshold,
                    # so congratulate him when he ranks up again
                    user.highest_rank = rank

                elif rank > user.highest_rank:
                    logger.debug(f"user {user} old rank {user.highest_rank}, new rank {rank}, sending congrats...")
                    await self._send_congrats(user, rank, image)
                    user.highest_rank = rank

    async def _sync_user_task(self, queue):
        first = True
        async for user_id in queue:
            if not first:
                delay = random.random() * 5.
                logger.debug(f"rate limiting: sleeping for {delay}s")
                await curio.sleep(delay)
            else:
                first = False
            session = self.database.Session()
            try:
                user = self.database.by_id(session, user_id)
                await self._sync_user(user)
            except Exception:
                logger.exception(f'exception while syncing {user.discord_id} {user.battle_tag}')
            finally:
                await queue.task_done()
                session.commit()
                session.close()            

    
    async def _sync_check(self):
        queue = curio.Queue()
        session = self.database.Session()
        try:
            ids_to_sync = self.database.get_to_be_synced(session)
        finally:
            session.close()
        logger.info(f"{len(ids_to_sync)} users need to be synced")
        if ids_to_sync:
            for user_id in ids_to_sync:
                await queue.put(user_id)
            async with curio.TaskGroup(name='sync users') as g:
                for _ in range(5):
                    await g.spawn(self._sync_user_task, queue)
                await queue.join()
                await g.cancel_remaining()
            logger.info("done syncing")

    async def _sync_all_users_task(self):
        await curio.sleep(10)
        logger.debug("started waiting...")
        while True:
            try:
                await self._sync_check()
            except Exception as e:
                logger.exception(f"something went wrong during _sync_check")
            await curio.sleep(60)



def fuzzy_nick_match(ann, ctx: Context, name: str):
    def strip_tags(name):
        return re.sub(r'^(.*?\|)?([^[]*)(\[.*)?', r'\2', str(name)).strip()

    member = member_id = None
    guild = ctx.bot.guilds[GUILD_ID]
    if name.startswith("<@") and name.endswith(">"):
        id = name[2:-1]
        if id[0] == "!":  # strip nicknames
            id = id[1:]
        try:
            member_id = int(id)
        except ValueError:
            raise ConversionFailedError(ctx, name, Member, "Invalid member ID")
    else:
        candidates = process.extract(name, {id: strip_tags(mem.name) for id, mem in guild.members.items()})
        if candidates:
            highest_score, group = next(groupby(candidates, key=itemgetter(1)))
            def sortkey(item):
                nick = item[0]
                if name.lower() == nick.lower():
                    return -101
                elif len(name) == len(nick):
                    return -100
                elif len(nick) < len(name):
                    return 100
                else:
                    return len(nick)
            
            if highest_score >= 50:
                group = sorted(group, key=sortkey)
                member, score, member_id = group[0]
                logger.debug(f"{member}, {score}")

    # allow two extra letters for fat fingering, but otherwise
    # if the nick is shorter then what we searched for,
    # it probably is not it
    if len(member) + 2 < len(name):
        raise ConversionFailedError(ctx, name, Member, 'Cannot find member with that name')

    if member_id is not None:
        member = guild.members.get(member_id)

    if member is None:
        raise ConversionFailedError(ctx, name, Member, 'Cannot find member with that name')
    else:
        return member
      
Context.add_converter(Member, fuzzy_nick_match)


multio.init('curio')

client = Client(BOT_TOKEN)

database = Database()

async def check_guild(guild):
    if guild.id != GUILD_ID:
        logger.info("Unknown guild! leaving")
        if guild.system_channel:
            await guild.system_channel.messages.send(f"I'm not configured for this guild! Bye!")
        try:
            await guild.leave()
        except:
            logger.fatal("unknown guild, but cannot leave it...")
            raise SystemExit(1)

@client.event('guild_join')
async def guild_join(ctx, guild):
    await check_guild(guild)

@client.event('ready')
async def ready(ctx):
    for guild in ctx.bot.guilds.copy().values():
        await check_guild(guild)

    await manager.load_plugin(Orisa, database)
    await ctx.bot.change_status(game=Game(name='"!bt help" for help'))
    logger.info("Ready")

@client.event('guild_member_remove')
async def remove_member(ctx: Context, member: Member):
    logger.debug(f"Member {member.name}({member.id}) left the guild")
    session = database.Session()
    try:
        user = database.by_discord_id(session, member.id)
        if user:
            logger.info(f"deleting {user} from database")
            session.delete(user)
            session.commit()
    finally:
        session.close()



manager = CommandsManager.with_client(client, command_prefix="!")

client.run(with_monitor=True)
