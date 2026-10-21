# GIDEON in plain words

GIDEON is a legal AI system that a federal defender office runs on a server it owns and controls.

## What GIDEON is and who it serves

GIDEON serves attorneys, investigators, paralegals, legal assistants, and support staff working on federal criminal defense and capital habeas cases. The office's technical staff run the box. Other defender offices adopt the system by installing a numbered release and supplying their own office settings.

One chat window offers two chats. General replaces a public chatbot for everyday work and can search the web; it does not verify its answers. Legal is for anything about a case or anything confidential: nothing typed or uploaded leaves the box. Legal brings together research over the court-decision collection and review of a matter's documents, separately or together in the same conversation. Research citations come from that fixed collection and are computed and verified before they appear, never recalled from the model's memory.

This overview describes the system as designed. The status line near the top of the [README](../README.md) states what is complete today.

## How its answers are checked

The answer key comes from attorneys: they rule on research questions, grade the relevance of passages, and sign off on reference answers. These rulings, grades, and sign-offs are recorded once. A correction becomes a new record that replaces the earlier one as the authority, preserving the history.

The nightly exam puts a fixed set of questions through the system and measures the answers against that key. It checks such things as finding the right sources, refusing unsafe calculations, and answering through the chat window. The judge is the same language model shipped to write answers; it grades synthesis quality against a reference answer and never decides a gate. An attorney panel audits a sample of its grading at every release that changes research behaviour.

The gates are fixed rules enforced by code. Research citations and quoted passages are checked against the collection before they are shown. On either chat, a code-enforced guardrail covers three families of calculation or confirmation: filing deadlines, Sentencing Guidelines ranges resolved to a months range, and sentence credit or release dates. When it detects one, it replaces the answer with a fixed refusal that defers to a person. Sums of money, percentages, and quantities are refused by instruction only; the exam measures and reports those refusals, but they never decide whether a release passes. Some release categories have zero tolerance: a single failure blocks, such as any citation unsupported by the collection. No case that passed in the reference run, the last release's run, may fail. Other measured scores are compared case by case against that reference using a statistical rule that blocks a release only when the comparison shows a clear decline. The model's grading decides none of these gates.

## The parts of the system

| Part | What it does and why it exists |
|---|---|
| The server | The office's server supplies the storage, memory, and graphics processors that keep the work local. Setup prepares the machine and checks its capacity and connections before the system starts. |
| The site file and the apply | The site file records the office's settings, such as sign-in, network, and backup arrangements. The apply turns those settings into a running system and checks that its services are healthy, giving each office the same product with its own connections. |
| The services on the box | Separate services handle chat requests, store records, run searches, and process background jobs. Their defined connections and memory limits let them work together, while the job queue lets interrupted work resume. |
| The chat assistant and the engine | The chat assistant is the window people use; the engine is the local language model that produces the words. GIDEON manages the conversation between them and applies its refusal rules before protected calculations reach the reader. |
| The caselaw collection | The collection is a recorded selection of court decisions whose downloaded contents are checked before use. Processing keeps the source text, separates the court's opinion from other sections, and records pages and citations so research can point back to a passage. |
| The search indexes | The indexes arrange the collection for searches by words and by meaning, with repeated passages grouped together. They are built from the saved record and text, can be rebuilt, and become the serving copy only after checks and a person's explicit decision. |
| Reading citations exactly | This part recognizes citations in text and preserves their exact wording and location. It keeps an identified case, statute, or rule tied to what was actually written, so later searches can seek that particular source. |
| The eval harness | The eval harness runs the exam, records results, and compares cases with the reference run. Its question sets and separate checks cover research, refusals, chat behaviour, and preparation of the collection, so a change can be judged on the work it affects. |
| Learning from use | Feedback, refusal records, and notices of changed sources feed proposals for improvement. A person reviews and acts on them; these loops cannot change answers, models, or the collection on their own. |
| Backups and recovery | Backups preserve the database, saved source material, uploads, settings, and search snapshots, with secrets encrypted. Recovery checks its source before stopping the live system, and recovery drills exercise the saved copies so a backup is more than a promise. |
| The front door and monitoring | The front door gives the office an encrypted connection to chat and the technical staff's monitoring pages. Health checks, dashboards, and alerts show service failures, capacity problems, and missed scheduled work so staff can respond. |
| Security | Office sign-in and matter permissions control access, while each service receives only the credentials and access it needs. Case text stays out of operational logs; Legal keeps it on the box, while General's web search requires the user's confirmed toggle before sending search text out. |
| The tests and tools | Automated tests check system rules and catch changes that break installation, service connections, or the public package. Tools also build the services, rehearse installation and recovery, and exercise chat and monitoring pages as a person would, so those paths are checked alongside the code. |

## Where to read more

- [Installing GIDEON and its current status](../README.md)
- [Running GIDEON](runbooks/start-here.md)
- [What changed in each release](release-notes/)
