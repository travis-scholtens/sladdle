import os

from flask import Flask, request
from flask_slacksigauth import slack_sig_auth

app = Flask(__name__)
app.config['SLACK_SIGNING_SECRET'] = None


@app.route("/", methods=['POST'])
@slack_sig_auth
def hello_world():
    return repr(request.form)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
