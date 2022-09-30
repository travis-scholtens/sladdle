import datetime
from dateutil import parser
import json
import os
import re

from flask import Flask, request, Response
from flask_slacksigauth import slack_sig_auth
import firebase_admin
from firebase_admin import firestore

# Application Default credentials are automatically created.
app = firebase_admin.initialize_app()
db = firestore.client()

app = Flask(__name__)
app.config['SLACK_SIGNING_SECRET'] = None


@app.route("/event", methods=['POST'])
@slack_sig_auth
def event():
  if request.json and 'challenge' in request.json:
    return request.json['challenge']
  return ''

def get_rankings(division, team, rank_type):
  ratings = (db.collection('rankings')
       .document('lipta')
       .collection('divisions')
       .document(division)
       .collection('teams')
       .document(team)).get()
  if not ratings.exists:
    return None
  data = ratings.to_dict()
  timestamp = f'previous_{rank_type}_time'
  previous = []
  if (timestamp in data and 
      datetime.datetime.now() - datetime.datetime.fromtimestamp(data[timestamp]/1000)
      < datetime.timedelta(days=5)):
    previous = list(data[f'previous_{rank_type}'].items())
  return (list(data[rank_type].items()), previous)

def sort_ranked(name_rating_pairs, reverse):
  return sorted(
      [(name, rating)
       for (name, rating) in name_rating_pairs
       if rating is not None],
      key=lambda name_rating: (name_rating[1], name_rating[0]),
      reverse=reverse)

def sort_unranked(name_rating_pairs):
  return sorted(
      [(name, rating)
       for (name, rating) in name_rating_pairs
       if rating is None],
      key=lambda name_rating: name_rating[0])

def get_movements(current, previous, reverse):
  current_names = [name for (name, _) in sort_ranked(current, reverse)]
  previous_names = [name for (name, _) in sort_ranked(previous, reverse)]
  common = set(current_names) & set(previous_names)
  current_names = [name for name in current_names if name in common]
  previous_names = [name for name in previous_names if name in common]
  current_ranks = {current_names[i]: i for i in range(len(current_names))}
  previous_ranks = {previous_names[i]: i for i in range(len(previous_names))}
  return {
      name: '↑' if current_ranks[name] < previous_ranks[name] else '↓'
      for name in common
      if current_ranks[name] != previous_ranks[name]}

def try_bold(name, home):
  return '*' if name in home else ''

def try_id(name, ids):
  return f'<@{ids[name]}>' if name in ids else name

def try_num(f):
  return f'{f:.1f}' if f else '-'

def ranking(division, team, other, rank_type, reverse):
  if team == 'teams':
    return '\n'.join(sorted(
        f'{t.id}: {t.to_dict()["name"]}'
        for t in  db.collection('rankings')
                       .document('lipta')
                       .collection('divisions')
                       .document(division)
                       .collection('teams').stream()))
  (pairs, previous) = get_rankings(division, team, rank_type)
  if not pairs:
    return "Couldn't find ratings for {team}"
  if other:
    home = {name for (name, _) in pairs}
    others = get_rankings(division, other, rank_type)
    if not others:
      return "Couldn't find ratings for {other}"
    pairs += others
    previous = []
  else:
    home = set()
  movement = get_movements(pairs, previous, reverse)
  ids = db.document('slack/names').get().to_dict() or {}
  ids = ids['ids'] if ids else ids
  return '\n'.join([
      f'{try_bold(name, home)}{movement.get(name, "·")} {try_id(name, ids)}, {try_num(pti)}{try_bold(name, home)}'
      for (name, pti) in
          sort_ranked(pairs, reverse) +
          sort_unranked(pairs)])

@app.route("/pti", methods=['POST'])
@slack_sig_auth
def pti():
  parts = request.form['text'].split()
  other = None
  if len(parts) > 1 and parts[-2] == 'vs':
    other = parts.pop()
    parts.pop()
  division = parts[0] if parts else 'd7'
  team = parts[-1] if parts else 'pwyc'
  if team == division:
    division = 'd7'
  return ranking(division, team, other, 'pti', False)


@app.route("/rank", methods=['POST'])
@slack_sig_auth
def rank():
  parts = request.form['text'].split()
  other = None
  if len(parts) > 1 and parts[-2] == 'vs':
    other = parts.pop()
    parts.pop()
  division = parts[0] if parts else 'd7'
  team = parts[-1] if parts else 'pwyc'
  if team == division:
    division = 'd7'
  return ranking(division, team, other, 'tskill', True)

def can_write(channel, user):
  doc = db.collection('channels').document(channel).get()
  if not doc.exists:
    return True
  val = doc.to_dict()
  if 'admins' not in val or not val['admins']:
    return True
  return user in val['admins']


def parse_date(d):
  try:
    int(d)
    return None
  except ValueError:
    pass
  d = d.lower()
  if d == 'yesterday':
    return datetime.date.today() - datetime.timedelta(days=1)
  if d == 'today':
    return datetime.date.today()
  if d == 'tomorrow':
    return datetime.date.today() + datetime.timedelta(days=1)
  try:
    date = parser.parse(d).date()
    if date.month <= 6 and date.year == datetime.date.today().year:
      return date + datetime.timedelta(years=1)
    return date
  except parser.ParserError:
    return None

def lineups(channel):
  return db.collection('channels').document(channel).collection('lineups')

def create(channel, user, date):
  if not can_write(channel, user):
    return f"<@{user}> can't do that"
  if not date:
    return 'Missing date'
        
  lineups(channel).document(str(date)).set({
        'play_on_date': str(date),
        'courts': { str(i): [None, None] for i in range(1, 7)}
    })
  return f'Started a new empty lineup for <#{channel}> on {date}'


def delete(channel, user, date):
  if not can_write(channel, user):
    return f"<@{user}> can't do that"

  if not date:
    return 'Missing date'
  
  lineup = by_date(channel, date)
  if not lineup:
    return f'There is no lineup for a match on {date}'
  lineup.reference.delete()
  return f'Removed lineup for <#{channel}> on {date}'


def show(channel, date):
  lineup = by_date(channel, date)
  if not lineup:
    if date:
      return f'There is no lineup for a match on {date}'
    else:
      return 'There are no upcoming match lineups'
  val = lineup.to_dict()
  not_full = ', '.join([str(c) for c in range (1, 7) if not all(val['courts'][str(c)])])
  if not_full:
    return (f'The match for <#{channel}>, to be played on {val["play_on_date"]}, '
          + f'still needs players on: {not_full}')
  else:
    return display(channel, date, False)


def by_date(channel, date):
  if date:
    by_play = lineups(channel).where('play_on_date', '==', str(date)).get()
    for lineup in by_play:
      return lineup
    by_id = lineups(channel).document(str(date)).get()
    if by_id.exists:
      return by_id
    return None
  next_match = (lineups(channel)
                   .where('play_on_date', '>=', str(datetime.date.today()))
                   .order_by('play_on_date')
                   .limit(1)).get()
  for lineup in next_match:
    return lineup
  return None

def assigned_msg(modifier, c, current, date):
  assigned = ' and '.join([n for n in current if n]) or 'Nobody'
  return f'{assigned} {modifier} playing on court {c} on {date}'

    
def court(channel, user, date, c, names):
  lineup = by_date(channel, date)
  if not lineup:
    return 'There are no upcoming match lineups'
  val = lineup.to_dict()
  current = val['courts'][str(c)]
  if not names:
    return assigned_msg('currently', c, current, val['play_on_date'])
  if not can_write(channel, user):
    return f"<@{user}> can't do that"
  if len(names) <= len([n for n in current if not n]):
    names.reverse()
    for i in range(2):
      if not names:
        break
      if not current[i]:
        current[i] = names.pop()
    lineup.reference.update(val)
    return assigned_msg('now', c, current, val['play_on_date'])
  elif len(names) == len([n for n in current if n]) == 2:
    for i in range(2):
      current[i] = names[i]
    lineup.reference.update(val)
    return assigned_msg('now', c, current, val['play_on_date'])
  return assigned_msg('already', c, current, val['play_on_date'])


times = { 7: [2,6],
          8: [1,4],
          9: [3,5] }

def md(text):
  return { 'type': 'mrkdwn', 'text': text }

def section(text, fields=None):
  s = { 'type': 'section', 'text': md(text) }
  if fields:
    s['fields'] = fields
  return s

divider = { 'type': 'divider' }

def field(text):
  return md(text)

def display(channel, date, in_channel=True):
  lineup = by_date(channel, date)
  if not lineup:
    return 'There are no upcoming match lineups'
  val = lineup.to_dict()
  if not any(sum([ps for ps in val['courts'].values()], [])):
    return show(channel, date)
  text = f'Lineup for <#{channel}> for match on {val["play_on_date"]}'
  display_date = f'{parser.parse(val["play_on_date"]):%B %d}'
  blocks = []
  blocks.append(section(f'*<#{channel}>* lineup for *{display_date}*'))
  blocks.append(divider)
  for (t, cs) in times.items():
    fields = []
    for c in cs:
      ps = val['courts'][str(c)]
      if not any(ps):
        continue
      content = f'{c}: {ps[0]}'
      if ps[1]:
        content += f'\n     {ps[1]}'
      fields.append(field(content))
    if fields:
      blocks.append(section(f'*{t}:00*', fields))
  return Response(
      json.dumps({ 'response_type': 'in_channel' if in_channel else 'ephemeral', 
                   'text': text,
                   'blocks': blocks }).replace('\\n', '\n'),
      mimetype='application/json')

id_pattern = re.compile('<@([^|]+)|.*>')
def get_id(user):
  m = id_pattern.match(user)
  if not m:
    return None
  return m[1]

def admin(channel, user, to_add):
  if not can_write(channel, user):
    return f"<@{user}> can't do that"
  doc = db.collection('channels').document(channel).get()
  val = doc.to_dict() if doc.exists else {}
  
  if 'admins' not in val:
    val['admins'] = []
  
  for u in to_add:
    id = get_id(u)
    if not id:
      continue
    if id not in val['admins']:
      val['admins'].append(id)
  if doc.exists:
    doc.reference.update(val)
  else:
    doc.reference.set(val)
  return ('No admins' if not val['admins']
          else ', '.join([f'<@{a}>' for a in val['admins']]))

def unadmin(channel, user, to_remove):
  if not can_write(channel, user):
    return f"<@{user}> can't do that"
  doc = db.collection('channels').document(channel).get()
  val = doc.to_dict() if doc.exists else {}
  
  if 'admins' not in val:
    val['admins'] = []
  
  for u in to_remove:
    id = get_id(u)
    if not id:
      continue
    if id in val['admins']:
      val['admins'].remove(id)
  if doc.exists:
    doc.reference.update(val)
  else:
    doc.reference.set(val)
  return ('No admins' if not val['admins']
          else ', '.join([f'<@{a}>' for a in val['admins']]))


@app.route("/lineup", methods=['POST'])
@slack_sig_auth
def lineup():
    channel = request.form['channel_id']
    ts = request.form['text'].split()
    date = None
    (maybe_date, cmds) = (ts[0] if ts else None, ts[1:])
    if maybe_date:
      date = parse_date(maybe_date)
      if not date:
        cmds.insert(0, maybe_date)
    if not cmds:
      return(show(channel, date))
    if cmds == ['new']:
      return create(channel, request.form['user_id'], date)
    if cmds == ['delete']:
      return delete(channel, request.form['user_id'], date)
    if cmds == ['view']:
      return display(channel, date)
    if not date and cmds[0] == 'admin':
      return admin(channel, request.form['user_id'], cmds[1:])
    if not date and cmds[0] == 'unadmin':
      return unadmin(channel, request.form['user_id'], cmds[1:])
    try:
      return court(channel, request.form['user_id'], date, int(cmds[0]), cmds[1:])
    except ValueError:
      return 'Expected a court number (1-6)'



if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
