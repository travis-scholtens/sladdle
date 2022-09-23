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


def create(channel, date):
  if not date:
    return 'Missing date'
        
  db.collection('lineups').collection(channel).document(str(date)).set({
        'play_on_date': str(date),
        'courts': { i: (None, None) for i in range(1, 7)}
    })
  return f'New empty lineup for {channel} on {date}'

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
    try:
      return court(channel, date, int(cmds[0]), cmds[1:])
    except ValueError:
      return 'Expected court number (1-6)'


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
