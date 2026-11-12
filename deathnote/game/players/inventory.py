"""Owned member cards shared by GetPlayerData, rewards and card actions."""
from __future__ import annotations

from deathnote.protocol.wire import integer_field


def new_member_card(master_id, *, owned_id=None, gained_at=0):
    return {'id': master_id if owned_id is None else owned_id, 'master_id': master_id,
            'awake_count': 1, 'card_rank': 1, 'leader_skill_level': 1,
            'live_skill_level': 1, 'performance_skill_level': 1,
            'gain_at': int(gained_at), 'link_skill_level': 0, 'gekisou_skill_level': 1}


def member_cards(state, created_at=0):
    """Migrate the previously implicit five starter cards into explicit ownership."""
    if 'member_cards' not in state:
        state['member_cards'] = {str(key): new_member_card(key, gained_at=created_at)
                                 for key in range(1, 6)}
    return state['member_cards']


def member_card_message(card, state):
    values = ((1, card['id']), (2, card['master_id']),
              (3, state['member_exp'].get(str(card['id']), 0)),
              (4, card['awake_count']), (5, card['card_rank']),
              (6, card['leader_skill_level']), (7, card['live_skill_level']),
              (8, card['performance_skill_level']), (9, card['gain_at']),
              (10, card['link_skill_level']), (11, card['gekisou_skill_level']))
    return b''.join(integer_field(number, value) for number, value in values if value)
