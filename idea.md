# Digital Code Giveaway Web App

giveaway.quest - a tool for giving away digital text assets like video game codes

## Problem

giving away extra steam video game codes is hard. Primarily due to two issues: only real verified people should be considered, it should be random and automated.

## Solution

giveaway.quest web app that allows login via mastodon for participating and hosting giveaways. 

User logins using "login with mastodon" and is then allowed to create a giveaway post and registers the digital asset (e.g. steam code, a text string like AAAA-BBBB-CCCC-DDDD) with basic minimal participation rules like what server account are allows to participate (default all) and how old the account should be (default all). In the post addendum the poster can specify additional conditions and limitations which participant has to "read agree with" before enrolling into giveaway which is primarily a tool to ensure that people who can actually use the asset are participating as some keys are locked regionally (e.g. "this giveaway only applies to US").

I'm not too familiar with Fediverse integrations but only post itself needs to be federated - preferably through "sign in through mastodon" server itself so we don't have to host or federate posts. The giveaway secret information would exclusively live on giveaway.quest servers.

The front page should display a paginated collection of all giveaways which can be sorted and filtered. The giveaway creator can mark giveaway as "show on giveaway.quest" (on by default).

The gimmick of the website is an ability to provide a quest to participants for the giveaway like "pet a cat" or "take a 20 minute walk" nothing serious just little engagement and wimsy. However we can keep this as part of integrated mastodon post e.g.:

```
@wraptile@fosstodon.org writes:

Psychonauts 2 steam key https://giveaway.quest/blue-jelly-cat just pet a cat to win :)
```

Then the post should show a rich preview from giveaway submission on giveaway.quest so we have to add rich preview markup to the page. For giveaway ids I'd like friendly random code generation like `blue-jelly-cat`. I suspect the volume will be rather small here so we can keep these short with some Python library that already creates random word-based ids.

The owner of the post should be able to edit the giveaway to change end time or delete it.

The front end page for giveaway should feature the post, quest and timer for when the giveaway ends and anything else that is necessary.

## Technical implementation

This is a pytho based web app with focus on minimal JS and static rendering (but pretty) where possible. The backend is Python https://litestar.dev/ with sqlite for storage. The front-end stack is not as important as long as it achieves the former requirement of minimal js and rendering. For front-end https://daisyui.com/ tailwind

I think the hardest challenge here is login with mastodon -> post -> bind it back to giveaway.quest backend. 

The front end should be dynamic responsive and support both mobile and desktop viewing

We should also consider legality and safety to prevent administration overhead. So limit user generated content to text (mastodon post) and CLI ability to hide/delete user posts for administrators (me). For CLI let's use cyclopts.

The site should be crawler friendly with robots.txt clearly indicating it's allowed to be crawled and provide a sitemap that lists all giveaways (that are not excluded by show on giveaway.quest setting)

## Availability

I already own giveaway.quest on porkbun domains. However to host this I'd need a small VPS that I don't have yet and I'm open to anything affordable which can be discussed once the project is done. Due to simple Python + SQLite +tailwind stack I think there are a lot of great hosting options here.

We should include hourly backup script for the database.
