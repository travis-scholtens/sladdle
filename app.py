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
    return dateutil.parser.parse(d).date()
  except dateutil.parser.ParserError:
    return None


@app.route("/lineup", methods=['POST'])
@slack_sig_auth
def lineup():
    ts = request.form['text'].split()
    maybe_date =
    if not len(ts):
      return(show())
    if len(ts) == 1:
      try:
        return(show((ts[0])))
      except dateutil.parser.ParserError:
        return 'Expected date, like "/lineup tomorrow"'
    db.collection(u'test').document(u'x').set({
        u'test': request.form['text']
    })
    return 'hi <@travis.scholtens>'


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
