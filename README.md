# Pond Diary Home Assistant Add-on Repository

This GitHub repository is structured as a Home Assistant add-on repository.

## Repository layout
- `repository.yaml`: repository metadata for Home Assistant.
- `pond_diary/`: the Pond Diary add-on.
- `pond_diary/config.yaml`: add-on manifest.
- `pond_diary/Dockerfile`: container definition.
- `pond_diary/run.sh`: add-on startup command.
- `pond_diary/app/server.py`: backend API and responsive frontend.

## Add-on features
- Log water test results with dates and notes.
- Record treatments or other products added to the pond.
- Upload pond photos with descriptions.
- View all entries in one reverse-chronological timeline.
- View water test metrics in an interactive chart with zoom and time-range controls.
- Persist data in the add-on `/data` folder using SQLite.
- Choose a default entry mode from the add-on configuration.
- Enable black mode from the add-on configuration.
- Open the app through Home Assistant ingress with the HA menu visible.
- Show the add-on as a sidebar entry in Home Assistant.

## Install in Home Assistant
1. Push this repository to GitHub.
2. In Home Assistant, go to Settings -> Add-ons -> Add-on Store.
3. Open the repository menu and add `https://github.com/Misiakufal/PondDiary`.
4. Open the `Pond Diary` add-on, build it, and start it.
5. Open the Configuration tab to set `default_mode` and `black_mode` if you want.
6. Open `Pond Diary` from the left Home Assistant menu or from the add-on page.

## Notes
- The left sidebar entry is enabled through the add-on manifest with Home Assistant ingress.
- Once it appears in the sidebar, Home Assistant lets the user pin or hide it from the normal UI controls.
