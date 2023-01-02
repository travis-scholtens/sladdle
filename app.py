from collections import namedtuple
import datetime
from dateutil import parser
import json
import os
import re
import slack

from flask import Flask, request, Response
from flask_slacksigauth import slack_sig_auth
import firebase_admin
from firebase_admin import firestore

# Application Default credentials are automatically created.
app = firebase_admin.initialize_app()
db = firestore.client()

app = Flask(__name__)
app.config['SLACK_SIGNING_SECRET'] = None

client = slack.WebClient(token=os.environ.get('SLACK_TOKEN'))

def ephemeral(text, blocks=None):
  client.chat_postEphemeral(
      channel=request.form['channel_id'],
      user=request.form['user_id'],
      text=text,
      blocks=blocks)
  return ''

def post(text, blocks=None):
  client.chat_postMessage(
      channel=request.form['channel_id'],
      user=request.form['user_id'],
      text=text,
      blocks=blocks)
  return ''

@app.route("/event", methods=['POST'])
@slack_sig_auth
def event():
  if request.json and 'challenge' in request.json:
    return request.json['challenge']
  print(request.json)
  return ''

TeamDefinition = namedtuple('TeamDefinition', ['league', 'division', 'team'])

def team_definition(channel_id):
  channel = db.collection('channels').document(channel_id).get()
  if not channel.exists:
    return None
  value = channel.to_dict()
  if any([field not in value for field in ('league', 'division', 'team')]):
    return None
  return TeamDefinition(value['league'], value['division'], value['team'])

def get_rankings(defn, rank_type):
  ratings = (db.collection('rankings')
       .document(defn.league)
       .collection('divisions')
       .document(defn.division)
       .collection('teams')
       .document(defn.team)).get()
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

def ranking(defn, other, rank_type, reverse):
  if defn.team == 'teams':
    return '\n'.join(sorted(
        f'{t.id}: {t.to_dict()["name"]}'
        for t in  db.collection('rankings')
                       .document(defn.league)
                       .collection('divisions')
                       .document(defn.division)
                       .collection('teams').stream()))
  (pairs, previous) = get_rankings(defn, rank_type)
  if not pairs:
    return "Couldn't find ratings for {defn.team}"
  if other:
    home = {name for (name, _) in pairs}
    (others, _) = get_rankings(other, rank_type)
    if not others:
      return "Couldn't find ratings for {other.team}"
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
  defn = team_definition(request.form['channel_id'])
  if not defn:
    return f'No team associated with <@{request.form["channel_id"]}>'
  other = None
  if len(parts) > 1 and parts[-2] == 'vs':
    other = parts.pop()
    parts.pop()
  division = parts[0] if parts else defn.division
  team = parts[-1] if parts else defn.team
  if team == division:
    division = defn.division
  return ephemeral(
      ranking(
          TeamDefinition(defn.league, division, team),
          TeamDefinition(defn.league, division, other) if other else None,
          'pti', False))


@app.route("/rank", methods=['POST'])
@slack_sig_auth
def rank():
  parts = request.form['text'].split()
  defn = team_definition(request.form['channel_id'])
  if not defn:
    return f'No team associated with <@{request.form["channel_id"]}>'
  other = None
  if len(parts) > 1 and parts[-2] == 'vs':
    other = parts.pop()
    parts.pop()
  division = parts[0] if parts else defn.division
  team = parts[-1] if parts else defn.team
  if team == division:
    division = defn.division
  return ephemeral(
      ranking(
          TeamDefinition(defn.league, division, team),
          TeamDefinition(defn.league, division, other) if other else None,
          'divtskill', True))

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
    today = datetime.date.today()
    if date.month <= 6 < today.month and date.year == today.year:
      return date + datetime.date(date.year + 1, date.month, date.day)
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
  
  doc = lineups(channel).document(str(date))
  if 'courts' in (doc.get().to_dict() or {}):
    return f'A lineup for <#{channel}> on {date} already exists'

  (doc.update if doc.get().exists else doc.set)({
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
      return { 'text': f'There is no lineup for a match on {date}' }
    else:
      return { 'text': 'There are no upcoming match lineups' }
  val = lineup.to_dict()
  message = None
  not_full = ', '.join([str(c) for c in range (1, 7) if not all(val['courts'][str(c)])])
  if not_full:
    message = (f'The match for <#{channel}>, to be played on {val["play_on_date"]}, '
             + f'still needs players on: {not_full}')
  return display(channel, date, False, message)


def by_date(channel, date, include_yesterday=False):
  if date:
    by_play = lineups(channel).where('play_on_date', '==', str(date)).get()
    for lineup in by_play:
      return lineup
    by_id = lineups(channel).document(str(date)).get()
    if by_id.exists:
      return by_id
    return None
  first_day = datetime.date.today()
  if include_yesterday:
    first_day -= datetime.timedelta(days=1)
  next_match = (lineups(channel)
                   .where('play_on_date', '>=', str(first_day))
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
  if len(names) == 1 and names[0] in current:
    for i in range(2):
      if current[i] == names[0]:
        current[i] = None
    lineup.reference.update(val)
    return assigned_msg('now', c, current, val['play_on_date'])
  return assigned_msg('already', c, current, val['play_on_date'])


times = { 7: [2,6],
          8: [1,4],
          9: [3,5] }

court_labels = ' ⓵⓶⓷⓸⓹⓺'

clocks = { 7: '🕖',
           8: '🕗',
           9: '🕘' }

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

def display(channel, date, in_channel=True, message=None):
  lineup = by_date(channel, date)
  if not lineup:
    return 'There are no upcoming match lineups'
  val = lineup.to_dict()
  if not any(sum([ps for ps in val['courts'].values()], [])) and not message:
    return show(channel, date)
  text = f'Lineup for <#{channel}> for match on {val["play_on_date"]}'
  display_date = f'{parser.parse(val["play_on_date"]):%B %d}'
  blocks = []
  blocks.append(section(f'*<#{channel}>* lineup for *{display_date}* at '+
                        ('home against ' if eval(val['home']) else '') +
                        '*' + val['opponent'] + '*'))
  blocks.append(divider)
  for (t, cs) in times.items():
    fields = []
    for c in cs:
      ps = val['courts'][str(c)]
      if not any(ps):
        continue
      content = f'{court_labels[c]}: {ps[0]}'
      if ps[1]:
        content += f'\n     {ps[1]}'
      fields.append(field(content))
    if fields:
      blocks.append(section(f'{clocks[t]} *{t}:00*', fields))
  if message:
    blocks.append(section(message))
  return { 'in_channel': in_channel, 
           'text': text,
           'blocks': blocks }

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
      response = show(channel, date)
      return post(response['text'], response.get('blocks')) if response.get('in_channel') else ephemeral(response['text'], response.get('blocks')) 
    if cmds == ['new']:
      return ephemeral(create(channel, request.form['user_id'], date))
    if cmds == ['delete']:
      return ephemeral(delete(channel, request.form['user_id'], date))
    if cmds == ['view']:
      response = display(channel, date)
      return post(response['text'], response.get('blocks')) if response.get('in_channel') else ephemeral(response['text'], response.get('blocks')) 
    if not date and cmds[0] == 'admin':
      return ephemeral(admin(channel, request.form['user_id'], cmds[1:]))
    if not date and cmds[0] == 'unadmin':
      return ephemeral(unadmin(channel, request.form['user_id'], cmds[1:]))
    try:
      return ephemeral(court(channel, request.form['user_id'], date, int(cmds[0]), cmds[1:]))
    except ValueError:
      return ephemeral('Expected a court number (1-6)')


def show_score(date, cmds):
  if not can_write(request.form['channel_id'], request.form['user_id']):
    return f"<@{request.form['user_id']}> can't do that"
  lineup = by_date(request.form['channel_id'], date, True)
  if not lineup:
    return 'No match ' + (f'on {date}' if date else 'upcoming')
  val = lineup.to_dict()
  m = re.match('([1-6]) ([WwLl])(?: ([-0-7 ]*))?', ' '.join(cmds))
  if not m:
    return 'Expected: /score (1-6) (W|L) [set results]'
  ps = [p for p in val['courts'][m[1]] if p]
  outcome = 'won' if m[2] in 'Ww' else 'lost'
  result = m.group(3)
  message = f'{ps[0]} and {ps[1]}' if len(ps) == 2 else 'We'
  message += f' {outcome} on court {m[1]}'
  if result:
    message += f', {result}'
  return post(message)

@app.route("/score", methods=['POST'])
@slack_sig_auth
def score():
    ts = request.form['text'].split()
    date = None
    (maybe_date, cmds) = (ts[0] if ts else None, ts[1:])
    if maybe_date:
      date = parse_date(maybe_date)
      if not date:
        cmds.insert(0, maybe_date)
    return show_score(date, cmds)

def create_availability(channel, date, args):
  if not date or len(args) != 2:
    return 'Need date and opponent'

  defn = team_definition(channel)
  if not defn:
    return f'No team associated with <@{channel}>'
  team_doc = (db.collection('rankings')
                       .document(defn.league)
                       .collection('divisions')
                       .document(defn.division)
                       .collection('teams')
                       .document(args[1]).get())
  if not team_doc.exists:
    return f'No team "{args[1]}"'

  doc = lineups(channel).document(str(date))
  if 'available' in (doc.get().to_dict() or {}):
    return f'Availability for <#{channel}> on {date} already exists'
  (doc.update if doc.get().exists else doc.set)({
        'play_on_date': str(date),
        'available': { '7': [], '8': [], '9': []  },
        'opponent': team_doc.to_dict()['name'],
        'home': str(args[0] == 'vs')
    })
  return f'Created availability record for {date}'

def mark_availability(channel, date, user, hours):
  match = by_date(channel, date)
  if not match:
    return 'No match ' + (f'on {date}' if date else 'upcoming')
  value = match.to_dict()
  if 'available' not in value:
    return f'No availability record for {match.id}'
  for hour in ('7', '8', '9'):
    if hour in hours:
      if user not in value['available'][hour]:
        value['available'][hour].append(user)
    else:
      if user in value['available'][hour]:
        value['available'][hour].remove(user)
  if hours:
    if user in value['available'].get('no', []):
      value['available']['no'].remove(user)
  else:
    no = value['available'].get('no', [])
    if user not in no:
      no.append(user)
    value['available']['no'] = no
  match.reference.update(value)
  return (f'<@{user}> is ' +
      ('*not* ' if not hours else '') +
      f'available for the {value["play_on_date"]} match at ' +
      ('home against ' if eval(value['home']) else '') +
      value['opponent'] +
      (f', able to play at {"/".join(sorted(hours))}PM' if hours else ''))

def availability(channel, date):
  match = by_date(channel, date)
  if not match:
    return 'No match ' + (f'on {date}' if date else 'upcoming')
  value = match.to_dict()
  if 'available' not in value:
    return f'No availability record for {match.id}'
  
  defn = team_definition(channel)
  if not defn:
    return f'No team associated with <@{channel}>'
  ratings = (db.collection('rankings')
       .document(defn.league)
       .collection('divisions')
       .document(defn.division)
       .collection('teams')
       .document(defn.team)).get()
  if not ratings.exists:
    return f'No roster for {defn.team}'
  ids = db.document('slack/names').get().to_dict() or {}
  ids = ids['ids'] if ids else ids
  remaining = {ids[name] for name in ratings.to_dict().get('pti', {}) if name in ids}
  
  rows = [f'Available for the {value["play_on_date"]} match at ' +
      ('home against ' if eval(value['home']) else '') +
      value['opponent'] + ':']
  for hour in ('7', '8', '9'):
    rows.append(
        f'{hour}PM: ' + 
        ', '.join([f'<@{user}>' for user in value['available'][hour]]))
    remaining -= set(value['available'][hour])
  rows.append(
        'No: ' + 
        ', '.join([f'<@{user}>' for user in value['available'].get('no', [])]))
  remaining -= set(value['available'].get('no', []))
  if remaining:
    rows.append(
        'Not responded: ' + 
        ', '.join([f'<@{user}>' for user in remaining]))
  return '\n'.join(rows)

@app.route("/available", methods=['POST'])
@slack_sig_auth
def available():
    channel = request.form['channel_id']
    user = request.form['user_id']
    cmds = request.form['text'].split()

    target_user = user
    (maybe_user, cmds) = (cmds[0] if cmds else None, cmds[1:])
    if maybe_user:
      target_user = get_id(maybe_user)
      if not target_user:
        target_user = user
        cmds.insert(0, maybe_user)

    date = None
    (maybe_date, cmds) = (cmds[0] if cmds else None, cmds[1:])
    if maybe_date:
      date = parse_date(maybe_date)
      if not date:
        cmds.insert(0, maybe_date)

    if not cmds:
      cmds.append('789')
    if cmds[0] == 'who':
      return ephemeral(availability(channel, date))
    if cmds[0] == 'no' and (target_user == user or can_write(channel, user)):
      return ephemeral(mark_availability(channel, date, target_user, []))
    if cmds[0] in ('vs', '@') and can_write(channel, user):
      return ephemeral(create_availability(channel, date, cmds))
    return ephemeral(mark_availability(channel, date, target_user, list(''.join(cmds))))

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
