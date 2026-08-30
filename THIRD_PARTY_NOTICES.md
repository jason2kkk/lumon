# Third-Party Notices

This file records the licensing review for material distributed with Lumon.
The Apache-2.0 license in `LICENSE` applies only to Lumon code and other
material for which the project has redistribution authority. It does not
override third-party licenses, trademark rules, or provider terms.

## Direct software dependencies

The direct runtime dependencies are tracked in `requirements.lock` and
`frontend/package-lock.json`. Their license metadata should be checked again
when dependencies are upgraded.

| Area | Packages | Reported license family |
| --- | --- | --- |
| Python | FastAPI, `tavily-python` | MIT |
| Python | Uvicorn, httpx | BSD-3-Clause |
| Python | OpenAI client | Apache-2.0 |
| Python | `python-dotenv` | BSD-3-Clause |
| Python | `defusedxml` | PSF-compatible / Python Software Foundation terms |
| Frontend | React, React DOM, Framer Motion, Vite, Tailwind CSS, ESLint | MIT |
| Frontend | `canvas-confetti` | ISC |
| Frontend | `lucide-react` | ISC |
| Frontend | TypeScript | Apache-2.0 |

This summary is not a substitute for each package's license text. The package
metadata and lockfiles are the source of truth for the exact versions used.

## Bundled static assets

The existing UI assets are intentionally retained so the open-source release
does not change the UI. The repository does not make separate license claims
for these asset groups:

- Product logo, favicon, and Lumon marks: `frontend/public/logo*`, `favicon.png`.
- Avatar images: `frontend/public/avatars/`.
- UI illustrations and PNG icons: `frontend/public/*.png`, `frontend/public/*.jpg`.
- SVG icon and logo files: `frontend/public/*.svg`.

Brand marks such as OpenAI, Claude, Reddit, Hacker News, Tavily, and
SensorTower are subject to their owners' trademark and brand guidelines. The
Apache-2.0 license does not grant permission to use those marks beyond what
their policies allow.
