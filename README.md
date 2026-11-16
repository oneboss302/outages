# outages.castafero.com

Live outages worldwide, matched to breaking news. Rebuilt every 15 minutes by GitHub Actions.

**Monitors**
- Internet connectivity: [IODA](https://ioda.inetintel.cc.gatech.edu/) (Georgia Tech) country and regional outage events
- Cloud and online services: about 20 public status pages, plus AWS Health, Google Cloud, Azure and Slack
- Hazards: USGS earthquakes (M5+), GDACS orange/red alerts, NWS extreme alerts

**News:** major outlet RSS feeds, Google News outage searches, BleepingComputer, The Record and The Register.

**Correlation:** a monitored outage is linked to headlines that name it (the service, country or hazard location) alongside outage keywords. Outage headlines that match no monitor are grouped by event under "Reported in the news" (banks, airlines, carriers, utilities...).

- `build.py`: everything (standard library only). Edit `STATUSPAGES`, `NEWS_FEEDS` and `GNEWS_QUERIES` at the top to change sources.
- `snapshot/`: committed only when the set of active outages changes, which keeps a history and keeps the scheduled workflow active.
- Manual refresh: Actions → Update outages → Run workflow.
