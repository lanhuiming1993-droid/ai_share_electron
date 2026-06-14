# WeRSS Official Account ID Handling

WeRSS subscription management must distinguish three different identifiers:

- WeChat original account id: usually `__biz` from a public-account article URL. Use it only as source metadata or as a search clue.
- WeRSS search candidate id: returned by WeRSS account search. Use this id when adding a new subscription.
- AlphaDesk/WeRSS subscription id: returned by the subscription list after an account has been added. Use this id for remove or backfill actions.

When the user sends only a screenshot of a public-account homepage, do not invent an id from the image. Extract the account name from the image first, then run WeRSS search and ask the user to choose a numbered candidate when multiple matches appear.

When authorization is expired, run `source_auth.py werss-login` and return the generated `MEDIA:/.../werss-login.png` image to the channel so the user can scan it directly in WeChat.
