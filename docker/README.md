# Local integration environment

Synapse and maubot for testing the plugin end to end.

1. **Generate the Synapse config** (one time):

   ```sh
   cd docker
   docker compose run --rm -e SYNAPSE_SERVER_NAME=localhost -e SYNAPSE_REPORT_STATS=no synapse generate
   ```

2. **Enable registration for local test users.** In `docker/data/synapse/homeserver.yaml`, set `enable_registration: true`, then start the stack:

   ```sh
   docker compose up -d
   ```

3. **Create users:**

   ```sh
   docker compose exec synapse register_new_matrix_user -c /data/homeserver.yaml http://localhost:8008
   ```

   Create one admin user for yourself and one regular user to act as the bot.

4. **Log maubot into the bot user** with `mbc auth`. Point it at `http://localhost:29316`, then sign in as the bot user.

5. **Build and upload the plugin** from the repository root:

   ```sh
   mbc build -u
   ```

**OAuth note:** Datadog only redirects OAuth sign-ins to `localhost`. With a `localhost` `public_url` (like `http://localhost:29316` here), the browser returns straight to maubot. On any other `public_url`, `auth.oauth_redirect: auto` switches to paste-back: the browser lands on a `localhost` page that won't load, and the user pastes that address to the bot in their DM. Personal access tokens (`!bits login token <PAT>`) work everywhere.
