import cachetools

from cogs5e.models.errors import CombatException, CombatNotFound, RequiresContext, ChannelInCombat, \
    CombatChannelNotFound, NoCombatants
from utils.functions import get_selection

COMBAT_TTL = 60 * 60 * 24 * 7  # 1 week TTL


class Combat:
    message_cache = cachetools.LRUCache(100)

    def __init__(self, channelId, summaryMsgId, dmId, options, ctx, combatants=None, roundNum=0, turnNum=0,
                 currentIndex=None):
        if combatants is None:
            combatants = []
        self._channel = channelId  # readonly
        self._summary = summaryMsgId  # readonly
        self._dm = dmId
        self._options = options  # readonly (?)
        self._combatants = combatants
        self._round = roundNum
        self._turn = turnNum
        self._current_index = currentIndex
        self.ctx = ctx
        self.sort_combatants()

    @classmethod
    def new(cls, channelId, summaryMsgId, dmId, options, ctx):
        return cls(channelId, summaryMsgId, dmId, options, ctx)

    @classmethod
    def from_ctx(cls, ctx):
        raw = ctx.bot.db.jget(f"{ctx.message.channel.id}.combat")
        if raw is None:
            raise CombatNotFound
        return cls.from_dict(raw, ctx)

    @classmethod
    def from_dict(cls, raw, ctx):
        combatants = []
        for c in raw['combatants']:
            if c['type'] == 'common':
                combatants.append(Combatant.from_dict(c, ctx))
            elif c['type'] == 'monster':
                combatants.append(MonsterCombatant.from_dict(c, ctx))
            elif c['type'] == 'player':
                combatants.append(PlayerCombatant.from_dict(c, ctx))
            else:
                raise CombatException("Unknown combatant type")
        return cls(raw['channel'], raw['summary'], raw['dm'], raw['options'], ctx, combatants, raw['round'],
                   raw['turn'], raw['current'])

    def to_dict(self):
        return {'channel': self.channel, 'summary': self.summary, 'dm': self.dm, 'options': self.options,
                'combatants': [c.to_dict() for c in self.get_combatants()], 'turn': self.turn_num,
                'round': self.round_num, 'current': self.index}

    @property
    def channel(self):
        return self._channel

    @property
    def summary(self):
        return self._summary

    @property
    def dm(self):
        return self._dm

    @property
    def options(self):
        return self._options

    @property  # private write
    def round_num(self):
        return self._round

    @property  # private write
    def turn_num(self):
        return self._turn

    @property  # private write
    def index(self):
        return self._current_index

    @property
    def current_combatant(self):
        return self.get_combatants()[self.index] if self.index is not None else None

    def get_combatants(self):
        return self._combatants

    def add_combatant(self, combatant):
        self._combatants.append(combatant)
        self.sort_combatants()

    def sort_combatants(self):
        current = self.current_combatant
        self._combatants = sorted(self._combatants, key=lambda k: (k.init, k.initMod), reverse=True)
        for n, c in enumerate(self._combatants):
            c.index = n
        self._current_index = current.index if current is not None else None

    def get_combatant(self, name):
        return next((c for c in self.get_combatants() if c.name == name), None)

    async def select_combatant(self, name):
        """
        Opens a prompt for a user to select the combatant they were searching for.
        :rtype: Combatant
        :param name: The name of the combatant to search for.
        :return: The selected Combatant, or None if the search failed.
        """
        matching = [(c.name, c) for c in self.get_combatants() if name.lower() in c.name.lower()]
        return await get_selection(self.ctx, matching)

    def advance_turn(self):
        if len(self.get_combatants()) == 0:
            raise NoCombatants

        changed_round = False
        if self.index is None:  # new round, no dynamic reroll
            self._current_index = 0
            self._round += 1
        elif self.index + 1 >= len(self.get_combatants()):  # new round, TODO: dynamic reroll
            self._current_index = 0
            self._round += 1
            changed_round = True
        else:
            self._current_index += 1

        self._turn = self.current_combatant.init
        return changed_round

    @staticmethod
    def ensure_unique_chan(ctx):
        if ctx.bot.db.exists(f"{ctx.message.channel.id}.combat"):
            raise ChannelInCombat

    def get_db_key(self):
        return f"{self.channel}.combat"

    def commit(self):
        """Commits the combat to db."""
        if not self.ctx:
            raise RequiresContext
        self.ctx.bot.db.jsetex(self.get_db_key(), self.to_dict(), COMBAT_TTL)

    def get_summary(self):
        """Returns the generated summary message content."""
        combatants = sorted(self.get_combatants(), key=lambda k: (k.init, k.initMod), reverse=True)
        outStr = "```markdown\n{}: {} (round {})\n".format(
            self.options.get('name') if self.options.get('name') else "Current initiative",
            self.turn_num, self.round_num)
        outStr += '=' * (len(outStr) - 13)
        outStr += '\n'
        for c in combatants:
            outStr += ("# " if self.index == c.index else "  ") + c.get_summary() + "\n"
        outStr += "```"
        return outStr

    async def update_summary(self):
        """Edits the summary message with the latest summary."""
        await self.ctx.bot.edit_message(await self.get_summary_msg(), self.get_summary())

    def get_channel(self):
        """Gets the Channel object of the combat."""
        if self.ctx:
            return self.ctx.message.channel
        else:
            chan = self.ctx.bot.get_channel(self.channel)
            if chan:
                return chan
            else:
                raise CombatChannelNotFound

    async def get_summary_msg(self):
        """Gets the Message object of the combat summary."""
        if self.summary in Combat.message_cache:
            return Combat.message_cache[self.summary]
        else:
            msg = await self.ctx.bot.get_message(self.get_channel(), self.summary)
            Combat.message_cache[msg.id] = msg
            return msg

    async def final(self):
        """Final commit/update."""
        self.commit()
        await self.update_summary()

    def end(self):
        """Ends combat in a channel."""
        self.ctx.bot.db.delete(self.get_db_key())


class Combatant:
    def __init__(self, name, controllerId, init, initMod, hpMax, hp, ac, private, resists, attacks, ctx, index=None,
                 notes=None, effects=None, *args, **kwargs):
        if resists is None:
            resists = {}
        if attacks is None:
            attacks = {}
        if effects is None:
            effects = []
        self._name = name
        self._controller = controllerId
        self._init = init
        self._mod = initMod  # readonly
        self._hpMax = hpMax  # optional
        self._hp = hp  # optional
        self._ac = ac  # optional
        self._private = private
        self._resists = resists
        self._attacks = attacks
        self._index = index  # combat write only; position in combat
        self.ctx = ctx
        self._notes = notes
        self._effects = effects

    @classmethod
    def new(cls, name, controllerId, init, initMod, hpMax, hp, ac, private, resists, attacks, ctx):
        return cls(name, controllerId, init, initMod, hpMax, hp, ac, private, resists, attacks, ctx)

    @classmethod
    def from_dict(cls, raw, ctx):
        effects = []  # TODO
        return cls(raw['name'], raw['controller'], raw['init'], raw['mod'], raw['hpMax'], raw['hp'], raw['ac'],
                   raw['private'], raw['resists'], raw['attacks'], ctx, raw['index'], raw['notes'], effects)

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, new_name):
        self.name = new_name

    @property
    def controller(self):
        return self._controller

    @controller.setter
    def controller(self, new_controller_id):
        self._controller = new_controller_id

    @property
    def init(self):
        return self._init

    @init.setter
    def init(self, new_init):
        self._init = new_init

    @property
    def initMod(self):
        return self._mod

    @property
    def hpMax(self):
        return self._hpMax

    @hpMax.setter
    def hpMax(self, new_hpMax):
        self._hpMax = new_hpMax
        if self._hp is None:
            self._hp = new_hpMax

    @property
    def hp(self):
        return self._hp

    @hp.setter
    def hp(self, new_hp):
        self._hp = new_hp

    @property
    def ac(self):
        return self._ac

    @ac.setter
    def ac(self, new_ac):
        self._ac = new_ac

    @property
    def isPrivate(self):
        return self._private

    @isPrivate.setter
    def isPrivate(self, new_privacy):
        self._private = new_privacy

    @property
    def resists(self):
        for k in ('resist', 'immune', 'vuln'):
            if not k in self._resists:
                self.resists[k] = []
        return self._resists

    @property
    def attacks(self):
        return self._attacks

    @property
    def index(self):
        return self._index

    @index.setter
    def index(self, new_index):
        self._index = new_index

    @property
    def notes(self):
        return self._notes

    @notes.setter
    def notes(self, new_notes):
        self._notes = new_notes

    def get_effects(self):
        return self._effects

    def controller_mention(self):
        return f"<@{self.controller}>"

    def on_turn(self):  # TODO: reduce effect counters by 1
        """
        A method called at the start of each of the combatant's turns.
        :return: None
        """
        return

    def get_summary(self):  # TODO
        """
        Gets a short summary of a combatant's status.
        :return: A string describing the combatant.
        """
        return f"{self.name} - {self.index}"

    def get_status(self, private=False):  # TODO
        """
        Gets the start-of-turn status of a combatant.
        :param private: Whether to return the full revealed stats or not.
        :return: A string describing the combatant.
        """
        return f"Placeholder status for {self.name}"

    def to_dict(self):
        return {'name': self.name, 'controller': self.controller, 'init': self.init, 'mod': self.initMod,
                'hpMax': self.hpMax, 'hp': self.hp, 'ac': self.ac, 'private': self.isPrivate, 'resists': self.resists,
                'attacks': self.attacks, 'index': self.index, 'notes': self.notes,
                'effects': [e.to_dict() for e in self.get_effects()], 'type': 'common'}


class MonsterCombatant(Combatant):
    def __init__(self, name, controllerId, init, initMod, hpMax, hp, ac, private, resists, attacks, ctx, index=None,
                 monster_name=None, notes=None, effects=None):
        super(MonsterCombatant, self).__init__(name, controllerId, init, initMod, hpMax, hp, ac, private, resists,
                                               attacks, ctx, index, notes, effects)
        self._monster_name = monster_name

    @classmethod
    def from_monster(cls, name, controllerId, init, initMod, private, monster, ctx, opts=None, index=None):
        monster_name = monster['name']
        hp = int(monster['hp'].split(' (')[0])
        ac = int(monster['ac'].split(' (')[0])

        resist = monster.get('resist', '').replace(' ', '').split(',')
        immune = monster.get('immune', '').replace(' ', '').split(',')
        vuln = monster.get('vulnerable', '').replace(' ', '').split(',')
        # fix npr and blug/pierc/slash
        if opts.get('npr'):
            for t in (resist, immune, vuln):
                for e in t:
                    for d in ('bludgeoning', 'piercing', 'slashing'):
                        if d in e.lower(): t.remove(e)
        for t in (resist, immune, vuln):
            for e in t:
                for d in ('bludgeoning', 'piercing', 'slashing'):
                    if d in e and not d == e.lower():
                        try:
                            t.remove(e)
                        except ValueError:
                            pass
                        t.append(d)

        resists = {'resist': resist, 'immune': immune, 'vuln': vuln}
        attacks = {}  # TODO

        return cls(name, controllerId, init, initMod, hp, hp, ac, private, resists, attacks, ctx, index,
                   monster_name)

    @classmethod
    def from_dict(cls, raw, ctx):
        inst = super(MonsterCombatant, cls).from_dict(raw, ctx)
        inst._monster_name = raw['monster_name']
        return inst

    @property
    def monster_name(self):
        return self._monster_name

    def to_dict(self):
        raw = super(MonsterCombatant, self).to_dict()
        raw['monster_name'] = self.monster_name
        raw['type'] = 'monster'
        return raw


class PlayerCombatant(Combatant):
    def __init__(self, name=None, notes=None, effects=None):
        super(PlayerCombatant, self).__init__()


class CombatantGroup:
    pass
