# GIDEON at a glance

GIDEON is an AI assistant for Federal Defender Organizations (FDOs) and Community Defender Organizations (CDOs), the offices that represent people in federal criminal and capital habeas cases. Their work upholds the Sixth Amendment's guarantee of counsel, and GIDEON is built to support the attorneys, investigators, paralegals, and staff who do it. Each office runs GIDEON on its own server, which the office's computer systems administrators (CSAs) install and maintain. Another office adopts it by installing a numbered release with its own settings.

## Two chats

People use GIDEON through one chat window that offers two chats. General is for everyday work in place of a public chatbot: it can search the web, and it does not check its answers. Legal is for anything about a case or anything confidential, and nothing typed or uploaded there leaves the server. Legal researches a collection of court decisions and reviews a matter's documents, and each citation it gives comes from that collection and is checked before it appears.

This page describes GIDEON as designed. The status line near the top of the [README](../README.md) says what works today.

## How answers are checked

Attorneys write the answer key: they rule on research questions, grade passages, and sign off on reference answers. Every night a fixed set of questions runs through GIDEON and is scored against that key. A release goes out only when it passes fixed rules enforced by code, such as every citation being found in the collection and no question that passed before failing now. The language model helps grade the quality of the writing, but its grades do not decide whether a release passes.

GIDEON refuses three kinds of calculation outright: filing deadlines, Sentencing Guidelines ranges, and release dates or sentence credit. It gives a fixed answer instead, which leaves the calculation to a person.

## The parts of the system

| Part | What it does |
|---|---|
| The server | The office's own server stores the data and runs the language model, so the work stays local. |
| The site file and the apply | The site file records the office's settings, such as sign-in, network, and backups. The apply is the step that turns those settings into a running system and checks that it is healthy. |
| The services on the server | Separate services handle chat, records, searches, and background jobs, and interrupted work resumes where it stopped. |
| The chat assistant and the engine | The chat assistant is the window people use, and the engine is the local language model that writes the answers. |
| The caselaw collection | The collection is a recorded selection of court decisions, checked when downloaded and kept with its pages and citations so an answer can point to a passage. |
| The search indexes | The indexes let GIDEON search the collection by words and by meaning, and a new index serves only after checks and a person's approval. |
| Reading citations exactly | GIDEON recognizes citations in text and keeps their exact wording and location, so a search can find that particular source. |
| The eval harness | The eval harness runs the nightly questions and compares each result with the last release's. |
| Learning from use | Feedback and refusals become proposals for a person to review, and nothing changes on its own. |
| Backups and recovery | Backups keep the database, documents, settings, and indexes, with secrets encrypted, and recovery drills show the copies can be restored. |
| The front door and monitoring | An encrypted entrance serves the chat and the staff's monitoring pages, and alerts report failures, capacity problems, and missed work. |
| Security | Office sign-in and matter permissions control access, case text stays out of logs, and General sends a search to the web only when the user has turned web search on. |
| The tests and tools | Automated tests and rehearsals check the system's rules, installation, recovery, and the chat as a person would use it. |

## Where to read more

- [Installing GIDEON and its current status](../README.md)
- [Running GIDEON](runbooks/start-here.md)
- [What changed in each release](release-notes/)
