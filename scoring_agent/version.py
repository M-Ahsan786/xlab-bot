"""The one place the app's version number lives.

Keep this in step with `installer/scoring-agent.iss` (#define AppVersion) and the About modal -
`build_app.ps1` refuses to build if they disagree, so they cannot drift apart.
"""
__version__ = "1.3.0"
