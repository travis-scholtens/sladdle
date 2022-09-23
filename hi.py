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


@app.route("/", methods=['POST'])
@slack_sig_auth
def hello_world():
    db.collection(u'test').document(u'x').set({
        u'test': unicode(request.form['text'])
    })
    return 'hi <@travis.scholtens>'


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
