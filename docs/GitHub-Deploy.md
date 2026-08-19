Setting up non-interactive GitHub push for MusicData Manager

To allow the application to push commits to your GitHub repository from a non-interactive environment (cloud worker / container), provide one of the following authentication options.

Option A — Use a Personal Access Token (recommended for simple setups)
- Create a PAT with `repo` scope.
- Set the environment variable `GITHUB_TOKEN` to the token value in the host or deployment platform.

PowerShell example:

```powershell
$env:GITHUB_TOKEN = "ghp_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
# Or set it permanently in your service's environment variables
```

Option B — Use `AGENT_AUTH_TOKEN` (alias)
- You may set `AGENT_AUTH_TOKEN` instead of `GITHUB_TOKEN` if preferred.

Option C — Configure a GitHub App (advanced)
- Create a GitHub App, install it on the target repository, and provide these environment variables:
  - `GITHUB_APP_ID`
  - `GITHUB_APP_INSTALLATION_ID`
  - `GITHUB_APP_PRIVATE_KEY` (PEM content)

Notes
- The application will embed a valid token into the `origin` remote URL before pushing. Do NOT commit tokens to your repo.
- If you don't provide a usable token, sync will fail with a clear message explaining how to set `GITHUB_TOKEN` or configure a GitHub App.

If you'd like, I can add a simple diagnostics HTTP route that returns current git remote and auth detection info to help debug deployment environments.