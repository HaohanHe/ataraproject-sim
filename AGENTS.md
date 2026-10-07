# Event repository

This repository owns https://create.gosim.org/survey26/.
Read MAINTAINING.md before changing deployment or routing.
Use Node.js 22. Keep the existing public base path and authentication callbacks.
Website changes belong here; the shared domain publisher lives in
`gosimfoundation/hackathon`. Run the production build before merging.
Do not run database migrations or redeploy backend workers as part of a website-only change.
