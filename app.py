import datetime
from dateutil import parser
import os

from flask import Flask, request
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

def create(channel, date):
  if not date:
    return 'Missing date'
        
  lineups(channel).document(str(date)).set({
        'play_on_date': str(date),
        'courts': { str(i): [None, None] for i in range(1, 7)}
    })
  return f'Started a new empty lineup for <#{channel}> on {date}'


def delete(channel, date):
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
  prefix = f'The match for <#{channel}>, to be played on {val["play_on_date"]}, '
  not_full = ', '.join([str(c) for c in range (1, 7) if not all(val['courts'][str(c)])])
  if not_full:
    return prefix + f'still needs players on: {not_full}'
  else:
    return prefix + 'has players on every court'


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

    
def court(channel, date, c, names):
  lineup = by_date(channel, date)
  if not lineup:
    return 'There are no upcoming match lineups'
  val = lineup.to_dict()
  current = val['courts'][str(c)]
  if not names:
    return assigned_msg('currently', c, current, val['play_on_date'])
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

def display(channel, date):
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
        content += f'\\n     {ps[1]}'
      fields.append(field(content))
    if fields:
      blocks.append(section(f'*{t}:00*', fields))
  return { 'text': text, 'blocks': blocks }

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
      return create(channel, date)
    if cmds == ['delete']:
      return delete(channel, date)
    if cmds == ['view']:
      return display(channel, date)
    try:
      return court(channel, date, int(cmds[0]), cmds[1:])
    except ValueError:
      return 'Expected a court number (1-6)'



if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
