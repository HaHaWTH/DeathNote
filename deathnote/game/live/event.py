"""Original free-LIVE skip event points, event drops and campaign drops."""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from deathnote.game.players.inventory import member_cards
from deathnote.protocol.wire import integer_field, message_field
from deathnote.protocol.decode import fields

JST = timezone(timedelta(hours=9))
INT_MAX = 0x7fffffff


def event_player_fields(state):
    return b''.join(message_field(25, integer_field(1, int(event_id))
        + integer_field(2, value['point']) + integer_field(3, value['medal'])
        + integer_field(4, value['box_gacha_step']))
        for event_id, value in sorted(state.get('events', {}).items(), key=lambda row: int(row[0])))


def active(row, now):
    def timestamp(value):
        return datetime.strptime(value, '%Y/%m/%d %H:%M:%S').replace(tzinfo=JST).timestamp()
    return ((not row['_startAt'] or timestamp(row['_startAt']) <= now)
            and (not row['_endAt'] or now < timestamp(row['_endAt'])))


class NativeLiveEvent:
    def __init__(self, master_directory):
        directory = Path(master_directory)
        def rows(name):
            return json.loads((directory / (name + '.json')).read_text('utf8'))['_allData']
        self.events = rows('MasterEvent')
        self.event_points = rows('MasterLiveEventPoint')
        self.event_rewards = rows('MasterLiveEventReward')
        self.effects = rows('MasterEventEffect')
        self.achievements = rows('MasterEventAchievementReward')
        self.loops = rows('MasterEventAchievementLoopReward')
        self.reward_resources = {row['_id']: row for row in rows('MasterReward')}
        self.achievement_present_type = next(row for row in rows('MasterPresentType') if row['_id'] == 28)
        self.campaigns = rows('MasterCampaign')
        self.campaign_rewards = rows('MasterLiveCampaignReward')
        self.members = {row['_id']: row for row in rows('MasterMemberCard')}
        self.supports = {row['_id']: row for row in rows('MasterSupportCard')}
        self.characters = {row['_id']: row for row in rows('MasterCharacter')}
        self.challenge_points = {row['_scoreRank']: row['_value'] for row in rows('MasterLiveChallengePoint')}
        self.challenge_event_points = rows('MasterChallengeLiveEventPoint')
        self.challenge_event_rewards = rows('MasterChallengeLiveEventReward')
        parameters = {row['_id']: row['_value'] for row in rows('MasterParameter')}
        self.initial_challenge_point = int(parameters['initial_challenge_point'])

    def initialize_challenge(self, state, *, now):
        owned = state.setdefault('challenge_lives', {})
        for event in self.events:
            if event['_eventType'] == 1 and active(event, now):
                owned.setdefault(str(event['_id']), {'point': self.initial_challenge_point})
                state.setdefault('events', {}).setdefault(str(event['_id']),
                    {'point': 0, 'medal': 0, 'box_gacha_step': 0})

    def gain_challenge_points(self, state, deck, *, rank, rates, now):
        self.initialize_challenge(state, now=now)
        for event in self.events:
            if event['_eventType'] != 1 or not active(event, now):
                continue
            bonus = self.bonus(event['_id'], 2, state, deck)
            gain = self.apply_bonus(self.challenge_points[rank], bonus, rates['_eventPointRate'])
            current = state['challenge_lives'][str(event['_id'])]
            current['point'] = min(INT_MAX, current['point'] + gain)

    @staticmethod
    def apply_bonus(amount, bonus, rate):
        return min(INT_MAX, amount * (10000 + bonus) * rate // 10000)

    @staticmethod
    def draw(rows, rng):
        groups = defaultdict(list)
        for row in rows:
            groups[row['_group']].append(row)
        selected = []
        for group in sorted(groups):
            value = rng.randrange(10000)
            cumulative = 0
            for row in groups[group]:
                cumulative += row['_probability']
                if value < cumulative:
                    selected.append(row)
                    break
        return selected

    def bonus(self, event_id, bonus_type, state, deck):
        owned_members = member_cards(state)
        cards = []
        for number, entry in fields(deck):
            if number != 3:
                continue
            slot = dict(fields(entry))
            member = owned_members[str(slot[3])]
            cards.append((2, member, self.members[member['master_id']]))
            support = state.get('support_cards', {}).get(str(slot.get(4, -1)))
            if support is not None:
                cards.append((3, support, self.supports[support['master_id']]))
        value = 0
        for resource_type, owned, master in cards:
            character_ids = [master['_characterID']] if resource_type == 2 else master['_characterIDs']
            band_ids = {self.characters[key]['_bandID'] for key in character_ids}
            for effect in self.effects:
                if (effect['_eventId'] != event_id or effect['_eventBonusType'] != bonus_type
                        or effect['_resourceTypeConstraint'] != resource_type):
                    continue
                if effect['_characterId'] > 0 and effect['_characterId'] not in character_ids:
                    continue
                if effect['_bandId'] > 0 and effect['_bandId'] not in band_ids:
                    continue
                if effect['_cardType'] and effect['_cardType'] != master['_cardType']:
                    continue
                if effect['_tagId'] > 0 and (resource_type == 3 or effect['_tagId'] not in master['_bestMusicTagIDs']):
                    continue
                field = '_memberCardId' if resource_type == 2 else '_supportCardId'
                if effect[field] > 0 and effect[field] != master['_id']:
                    continue
                value += effect['_rank' + str(owned['card_rank']) + 'EffectValue']
        return value

    def settle(self, state, deck, *, rank, rates, player_rank, now, rng,
               challenge_event_id=None, live_type=1):
        event_wire, resources, achievement_resources = b'', [], []
        for event in self.events:
            if not active(event, now):
                continue
            if challenge_event_id is not None and event['_id'] != challenge_event_id:
                continue
            point_rows = self.event_points if challenge_event_id is None else self.challenge_event_points
            point_group = event['_liveEventPointGroup'] if challenge_event_id is None else event['_challengeLiveEventPointGroup']
            points = next(row['_value'] for row in point_rows
                          if row['_group'] == point_group and row['_scoreRank'] == rank)
            point_bonus = self.bonus(event['_id'], 0, state, deck)
            point_gain = self.apply_bonus(points, point_bonus, rates['_eventPointRate'])
            progress = state.setdefault('events', {}).setdefault(str(event['_id']),
                {'point': 0, 'medal': 0, 'box_gacha_step': 0})
            before_point = progress['point']
            progress['point'] = min(INT_MAX, progress['point'] + point_gain)
            claimed = progress.get('achievement_reward_ids', [])
            for achievement in self.achievements:
                if (achievement['_eventId'] != event['_id'] or achievement['_id'] in claimed
                        or not before_point < achievement['_eventPoint'] <= progress['point']):
                    continue
                for reward_id in achievement['_rewardIds']:
                    resource = self.reward_resources[reward_id]
                    achievement_resources.append((resource['_resourceType'], resource['_resourceId'], resource['_resourceCount']))
                claimed.append(achievement['_id'])
                progress['achievement_reward_ids'] = claimed
            for loop in self.loops:
                if loop['_eventId'] != event['_id']:
                    continue
                def count(point):
                    return max(0, (point - loop['_loopStartEventPoint']) // loop['_loopEventPoint'])
                before_count, after_count = count(before_point), count(progress['point'])
                granted = max(before_count, progress.get('loop_reward_count', 0))
                if after_count > granted:
                    for reward_id in loop['_rewardIds']:
                        resource = self.reward_resources[reward_id]
                        achievement_resources.append((resource['_resourceType'], resource['_resourceId'],
                                                      resource['_resourceCount'] * (after_count - granted)))
                    progress['loop_reward_count'] = after_count
            item_bonus = self.bonus(event['_id'], 1, state, deck)
            reward_rows = self.event_rewards if challenge_event_id is None else self.challenge_event_rewards
            reward_group = event['_liveEventRewardGroup'] if challenge_event_id is None else event['_challengeLiveEventRewardGroup']
            rewards = self.draw((row for row in reward_rows
                if row['_eventGroup'] == reward_group and row['_scoreRank'] == rank), rng)
            for reward in rewards:
                count = self.apply_bonus(reward['_resourceCount'], item_bonus, rates['_liveMusicRewardRate'])
                resources.append((reward['_resourceType'], reward['_resourceId'], count))
                event_wire += message_field(2, integer_field(1, event['_id']) + integer_field(2, reward['_id']))
        campaign_resources = []
        live_type = live_type if challenge_event_id is None else 4
        for campaign in self.campaigns:
            if (campaign['_campaignType'] != 12 or campaign['_liveType'] not in (0, live_type)
                    or not active(campaign, now)
                    or campaign['_targetUserRank'] and player_rank > campaign['_targetUserRank']):
                continue
            rewards = self.draw((row for row in self.campaign_rewards
                if row['_campaignGroup'] == campaign['_parameter'] and row['_liveType'] == live_type
                and row['_liveScoreRank'] == rank), rng)
            campaign_resources.extend((row['_resourceType'], row['_resourceId'], row['_resourceCount'])
                                       for row in rewards)
        campaign_wire = b''.join(message_field(4, integer_field(1, kind)
            + integer_field(2, resource_id) + integer_field(3, count))
            for kind, resource_id, count in campaign_resources)
        posted = []
        for resource_type, resource_id, count in achievement_resources:
            present_id = state.get('last_present_id', 0) + 1
            state['last_present_id'] = present_id
            present = {'present_id': present_id, 'post_at': now,
                'expire_at': now + self.achievement_present_type['_expireDays'] * 86400,
                'resource_type': resource_type, 'resource_id': resource_id,
                'resource_count': count, 'present_type_id': 28, 'param_1': '', 'param_2': ''}
            state.setdefault('present_box', {})[str(present_id)] = present
            posted.append(present)
        return event_wire + campaign_wire, resources + campaign_resources, posted

    def settle_battle(self, state, deck, *, score_rank, rates, player_rank, now, rng):
        return self.settle(state, deck, rank=score_rank, rates=rates,
                           player_rank=player_rank, now=now, rng=rng, live_type=3)
