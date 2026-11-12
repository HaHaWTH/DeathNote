"""Permanent unlocks and monthly-pass state in the caller's player transaction."""
from __future__ import annotations

import json
from pathlib import Path
import time

from deathnote.protocol.rpc import NativeRpcError
from deathnote.game.calendar import NativeServiceCalendar
from .costume import current_costume_message, unlocked_costume_message
from deathnote.protocol.wire import integer_field, message_field, varint


SUBSCRIPTION = 6
STAMP = 9
PREMIUM_PASS = 10
CHAT_THEME = 1001
CHAT_BUBBLE = 1002
CHAT_FRAME = 1003
ENTITLEMENT_RESOURCE_TYPES = frozenset((SUBSCRIPTION, STAMP, PREMIUM_PASS,
                                      CHAT_THEME, CHAT_BUBBLE, CHAT_FRAME))
DAY = 86400
SERVICE_OFFSET = 9 * 3600


def _service_day(timestamp):
    return (timestamp + SERVICE_OFFSET) // DAY


def monthly_pass_message(value):
    return b''.join(integer_field(number, int(value[key]))
                    for number, key in enumerate((
                        'monthly_pass_id', 'purchased_at', 'expire_at',
                        'last_daily_reward_day_number', 'group_purchase_count',
                        'free_gacha_used_at', 'is_extension_purchased'), 1)
                    if value[key])


def _daily_message(value):
    return (integer_field(1, value['monthly_pass_id'])
            + integer_field(2, value['day_number'])
            + integer_field(3, value['target_at']))


class NativeEntitlements:
    resource_types = ENTITLEMENT_RESOURCE_TYPES

    def __init__(self, master_directory):
        directory = Path(master_directory)
        self.calendar = NativeServiceCalendar(directory)

        def rows(name):
            return json.loads((directory / (name + '.json')).read_text('utf8'))['_allData']

        self.monthly_passes = {row['_id']: row for row in rows('MasterMonthlyPass')}
        self.stamps = {row['_id']: row for row in rows('MasterStamp')}
        self.costume_groups = {row['_id']: row for row in rows('MasterCharacterCostumeGroup')}
        self.season_passes = {row['_id']: row for row in rows('MasterSeasonPass')}
        self.chat_masters = {
            CHAT_THEME: {row['_id']: row for row in rows('MasterBiliChatTheme')},
            CHAT_BUBBLE: {row['_id']: row for row in rows('MasterBiliChatBubble')},
            CHAT_FRAME: {row['_id']: row for row in rows('MasterBiliChatFrame')},
        }
        self.purchase_rewards = rows('MasterMonthlyPassPurchaseReward')
        self.first_rewards = rows('MasterMonthlyPassFirstTimeReward')
        self.continuation_rewards = rows('MasterMonthlyPassContinuationReward')
        self.daily_rewards = {}
        for row in rows('MasterMonthlyPassDailyReward'):
            self.daily_rewards.setdefault((row['_monthlyPassId'], row['_dayCount']), []).append(row)
        parameters = {row['_id']: row['_value'] for row in rows('MasterParameter')}
        self.add_purchase_threshold = int(parameters['monthly_pass_add_purchase_threshold'])

    @staticmethod
    def _resources(rows):
        return [(row['_resourceType'], row['_resourceId'], row['_resourceCount']) for row in rows]

    def _due_daily(self, value, now):
        master = self.monthly_passes[value['monthly_pass_id']]
        purchased_day = self.calendar.day(value['purchased_at'])
        available = min(master['_expireDays'], self.calendar.day(now) - purchased_day + 1)
        result = []
        for day_number in range(value['last_daily_reward_day_number'] + 1, available + 1):
            result.append({'monthly_pass_id': value['monthly_pass_id'],
                           'day_number': day_number,
                           'target_at': max(value['purchased_at'],
                                            (purchased_day + day_number - 1) * DAY - self.calendar.offset)})
        if result:
            value['last_daily_reward_day_number'] = result[-1]['day_number']
        return result

    def _settle(self, state, now):
        """Roll a prepaid next period when its original raw expiry is reached."""
        for value in state.get('monthly_passes', {}).values():
            if value['is_extension_purchased'] and value['expire_at'] <= now:
                pending = state.setdefault('monthly_pass_pending_daily_rewards', [])
                pending.extend(self._due_daily(value, value['expire_at'] - 1))
                value['purchased_at'] = value['expire_at']
                value['expire_at'] += self.monthly_passes[value['monthly_pass_id']]['_expireDays'] * DAY
                value['last_daily_reward_day_number'] = 0
                value['is_extension_purchased'] = False

    def _purchase_monthly_pass(self, state, pass_id, count, now):
        master = self.monthly_passes[pass_id]
        if count != 1:
            raise NativeRpcError(9)
        passes = state.setdefault('monthly_passes', {})
        group = master['_group']
        if any(value['monthly_pass_id'] != pass_id and value['expire_at'] > now
               and self.monthly_passes[value['monthly_pass_id']]['_group'] == group
               for value in passes.values()):
            raise NativeRpcError(9)
        group_count = max((value['group_purchase_count'] for value in passes.values()
                           if self.monthly_passes[value['monthly_pass_id']]['_group'] == group), default=0)
        previous = passes.get(str(pass_id))
        if previous is not None and previous['expire_at'] > now:
            extended_expire = previous['expire_at'] + (
                master['_expireDays'] * DAY if previous['is_extension_purchased'] else 0)
            if now < extended_expire - self.add_purchase_threshold * DAY:
                raise NativeRpcError(9)
            previous['is_extension_purchased'] = True
            previous['group_purchase_count'] = group_count + 1
        else:
            if previous is not None:
                state.setdefault('monthly_pass_pending_daily_rewards', []).extend(
                    self._due_daily(previous, previous['expire_at'] - 1))
            passes[str(pass_id)] = {
                'monthly_pass_id': pass_id, 'purchased_at': now,
                'expire_at': now + master['_expireDays'] * DAY,
                'last_daily_reward_day_number': 0, 'group_purchase_count': group_count + 1,
                'free_gacha_used_at': previous['free_gacha_used_at'] if previous else 0,
                'is_extension_purchased': False,
            }
        followups = self._resources(row for row in self.purchase_rewards if row['_monthlyPassId'] == pass_id)
        if group_count == 0:
            followups += self._resources(row for row in self.first_rewards if row['_monthlyPassGroup'] == group)
        followups += self._resources(row for row in self.continuation_rewards
                                     if row['_monthlyPassGroup'] == group and row['_purchaseCount'] == group_count + 1)
        return followups

    def apply(self, state, resources, *, now=None):
        acquired_at = int(time.time()) if now is None else int(now)
        self._settle(state, acquired_at)
        followups = []
        chat_keys = {CHAT_THEME: 'chat_themes', CHAT_BUBBLE: 'chat_bubbles', CHAT_FRAME: 'chat_frames'}
        for resource_type, resource_id, count in resources:
            if resource_type == SUBSCRIPTION:
                followups.extend(self._purchase_monthly_pass(state, resource_id, count, acquired_at))
            elif resource_type == STAMP:
                master = self.stamps[resource_id]
                if not master['_isInitialOwnership']:
                    state.setdefault('stamps', {}).setdefault(str(resource_id), {
                        'id': resource_id, 'favorites': [], 'got_at': acquired_at})
            elif resource_type == PREMIUM_PASS:
                self.season_passes[resource_id]
                state['premium_passes'] = sorted(set(state.get('premium_passes', ())) | {resource_id})
            elif resource_type in chat_keys:
                self.chat_masters[resource_type][resource_id]
                key = chat_keys[resource_type]
                state[key] = sorted(set(state.get(key, ())) | {resource_id})
            else:
                raise NotImplementedError('Entitlement resource type: ' + str(resource_type))
        return followups

    def _monthly_fields(self, state, number):
        return b''.join(message_field(number, monthly_pass_message(value))
                        for _, value in sorted(state.get('monthly_passes', {}).items(), key=lambda row: int(row[0])))

    def initialize_stamps(self, state):
        owned = state.setdefault('stamps', {})
        for stamp_id, master in self.stamps.items():
            if master['_isInitialOwnership']:
                owned.setdefault(str(stamp_id), {'id': stamp_id, 'favorites': []})

    def unlock_member_costumes(self, state, master_id, *, now):
        """Special-condition groups are granted with their owning member card."""
        gained = []
        for group in self.costume_groups.values():
            if group['_specialConditionMemberCardId'] != master_id:
                continue
            owned = state.setdefault('character_unlocked_costumes', {})
            key = str(group['_id'])
            if key not in owned:
                record = {'character_id': group['_characterID'],
                          'costume_group_id': group['_id'], 'got_at': int(now)}
                owned[key] = record
                gained.append(record)
        return gained

    def initialize_costumes(self, state):
        """Restore pre-existing cards' unlocks using their original gain time."""
        for card in state['member_cards'].values():
            self.unlock_member_costumes(state, card['master_id'], now=card['gain_at'])

    @staticmethod
    def costume_player_fields(state):
        owned = state.get('character_unlocked_costumes', {})
        current = state.get('character_current_costumes', {})
        return (b''.join(message_field(61, current_costume_message(value))
                         for _, value in sorted(current.items()))
                + b''.join(message_field(62, unlocked_costume_message(owned[key]))
                            for key in sorted(owned, key=int)))

    def player_fields(self, state, *, now=None):
        now = int(time.time()) if now is None else int(now)
        self._settle(state, now)
        self.initialize_stamps(state)
        self.initialize_costumes(state)
        result = self._monthly_fields(state, 52)
        for _, value in sorted(state['stamps'].items(), key=lambda row: int(row[0])):
            stamp = integer_field(1, value['id'])
            for favorite in value['favorites']:
                stamp += message_field(2, integer_field(1, favorite['favorite_id'])
                                       + integer_field(2, favorite['slot_index'] + 1))
            if 'got_at' in value:
                stamp += integer_field(3, value['got_at'])
            result += message_field(27, stamp)
        for number, key in ((44, 'chat_themes'), (45, 'chat_bubbles'), (46, 'chat_frames'),
                            (54, 'background_ids')):
            values = state.get(key, ())
            if values:
                result += message_field(number, b''.join(varint(value) for value in sorted(set(values))))
        return result + self.costume_player_fields(state)

    def home(self, state, *, now=None):
        now = int(time.time()) if now is None else int(now)
        self._settle(state, now)
        daily = state.pop('monthly_pass_pending_daily_rewards', [])
        for _, value in sorted(state.get('monthly_passes', {}).items(), key=lambda row: int(row[0])):
            daily.extend(self._due_daily(value, min(now, value['expire_at'] - 1)))
        daily.sort(key=lambda row: (row['target_at'], row['monthly_pass_id'], row['day_number']))
        followups = []
        for value in daily:
            followups.extend(self._resources(self.daily_rewards.get((value['monthly_pass_id'], value['day_number']), ())))
        response = self._monthly_fields(state, 8) + b''.join(message_field(9, _daily_message(value)) for value in daily)
        return response, followups

    def gacha_access(self, state, product, now):
        now = int(now)
        self._settle(state, now)
        active = [value for value in state.get('monthly_passes', {}).values()
                  if value['expire_at'] > now]
        entitled = any(value['monthly_pass_id'] in product['_monthlyPassIds'] for value in active)
        daily_free = (product['_itemType'] == 12 and entitled
                      and not any(self.calendar.day(value['free_gacha_used_at']) >= self.calendar.day(now)
                                  for value in active))
        return entitled, daily_free

    def consume_free_gacha(self, state, now):
        now = int(now)
        self._settle(state, now)
        for value in state.get('monthly_passes', {}).values():
            if value['expire_at'] > now:
                value['free_gacha_used_at'] = now
