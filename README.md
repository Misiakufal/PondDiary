# Pond Diary Home Assistant Add-on

This add-on provides a simple pond journal that works in desktop and mobile browsers.

## Features
- Log water test results with the test date and notes.
- Record treatments or other products added to the pond.
- Upload pond photos with a description.
- View all entries in one reverse-chronological timeline.
- Persist data in the add-on `/data` folder using SQLite.

## Files
- `config.yaml`: Home Assistant add-on metadata.
- `Dockerfile`: container definition.
- `run.sh`: add-on startup command.
- `app/server.py`: backend API and responsive frontend.

## Install
1. Copy this folder into your Home Assistant add-ons directory or a local add-on repository.
2. Add the repository in Home Assistant if needed.
3. Build and start the `Pond Diary` add-on.
4. Open the web UI on port `8099` from Home Assistant.

## Storage
Database and uploaded photos are stored in the add-on data directory, so entries survive restarts and upgrades.
