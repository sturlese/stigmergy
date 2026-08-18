Nexus reconciliation behaviour after the platform update

Finance walked me through what changed in the settlement runs after the last Nexus update, and
the short version is that nothing is wrong but the reconciliation now has to be read differently.

Before the update, a run that could not be matched came back as a single unmatched total and
somebody went looking for the cause by hand. Nexus now splits that total into the reasons it
could not match, and the reasons are its own categories rather than ours. So the number that used
to be checked at the end of a run no longer exists as one number, and a person looking for it
reads the largest category instead and quietly draws the wrong conclusion.

Finance is not asking for the old behaviour back. Their point is that the categories are useful
and the habit built around the old shape is not, so what needs writing down is how the new
reconciliation is meant to be read — which categories are ordinary, which are worth chasing, and
which have never once appeared.

They also noted that the change arrived in the platform's release notes rather than in any
message to us, which is where they now look first when a run behaves unexpectedly. That is worth
keeping as the habit rather than as an observation about one release.

Nothing was decided about changing our own process yet. This is a record of what moved and why
the old check no longer means what it used to.
