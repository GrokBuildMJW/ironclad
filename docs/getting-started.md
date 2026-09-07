# Getting started

This repository is documentation. It does not ship the engine, Ink, or
container images. ironclad-ai is self-hosted and currently distributed
to operators who already have the product. When you have it, this is the
shape of a first run.

## The short path

1. Resolve a workspace directory. Work-state will live at `.ironclad-ai/`
   beneath it.
2. Start the engine and open Ink:

   ```text
   ironclad run
   ```

   First launch creates the per-user runtime. Later launches attach to a
   matching loopback server or start the existing runtime. They do not
   rebuild on every call — that is `ironclad update`.
3. Type the work. When the run waits, approve from Ink.
4. Watch the same run in a browser at `http://127.0.0.1:8484/console`.
5. Inspect or pin processes at `/catalog` (`ironclad serve --catalog`).

![Ink after launch](../images/ink.png)

## Host split

| You are on | Engine | What you run locally |
|---|---|---|
| Linux | Native engine is supported. Docker is fine too. | `ironclad run` |
| Windows | Docker is the documented engine path. An operator can also enable a native `integration` runtime. | Native Ink against that engine |
| macOS | Docker engine | Native Ink against that engine |

The Windows installer, when you have it, puts Ink on your `PATH` as
`ironclad-ink`. It does not install a native engine service. Connect it
to the Docker (or operator-managed) engine:

```text
ironclad-ink --base-url http://127.0.0.1:8484
```

## Network boundary

Default bind is loopback, port `8484`. A non-loopback bind requires an
active bearer-token profile. The server refuses to open the socket without
one. Create a token with `ironclad auth token create`, store it outside
git, and pass it to Ink or the Python client.

## Catalog and console

```text
ironclad serve --catalog
```

Prints the catalog URL and holds the server. Open it, select
`sw_dev_default`, look at the flow, then go back to Ink to actually run
work. The catalog pins rulebooks. It does not approve live steps.

The console is an observer. Enable browser chat only if you want a second
seat on the same queue. Approvals still belong in Ink.

![Catalog](../images/catalog-library.png)

![Console](../images/console-operator.png)

## Configuration

User configuration is not workspace state:

```text
ironclad config view
ironclad config persist llm.orchestrator_profile local
```

Workspace selection is `--state-root`, `IRONCLAD_STATE_ROOT`, or the
durable user-root workspace pin. Changing directories does not silently
select another workspace once that pin exists.

## What this page will not pretend

- There is no public `pip install ironclad-ai` from this repository.
- There is no public image tag in this repository. Docker distribution
  uses an operator-managed private registry.
- Ink is the terminal client. There is not a second terminal product to install.
- The product is under active development. Commands, gates, and shipped
  processes match the current engine — not a future brochure.

If you want access, start at [https://ironclad-ai.ae/](https://ironclad-ai.ae/).
