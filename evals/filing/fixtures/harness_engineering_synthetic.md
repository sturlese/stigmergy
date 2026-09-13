# Harness Engineering: explained from zero

This synthetic fixture describes harness engineering: the operational layer that gives an AI
agent instructions, tools, feedback, and durable memory so it can complete a task reliably.

Santi (@santtiagom_) authored this explanation. A model alone is not an agentic system: the model
only takes text as input and supplies reasoning, while the harness turns those choices into work.
The article separates six capabilities supplied to the model from three trust capabilities supplied
to the user. The complete capability framework is: (1) tools let the model request named actions;
(2) a loop repeats decide, act, observe, and decide again; (3) memory/state preserves work outside
the stateless model; (4) context selection chooses what the model sees on each call; (5) a working
environment or isolated workspace contains execution; and (6) a clear objective and verification
turn completion into external acceptance criteria.

The complete trust framework is: (7) permissions and limits determine what tools can run and what
requires approval; (8) observability preserves traces of context, calls, parameters, and outcomes;
and (9) evals use stable evaluation tasks to compare harness versions and catch regressions.

Skills, MCP, subagents, and long-term memory are extensions of the same harness design rather than
replacements for the model-request and harness-execution pattern.

The source post identifier is 2098782814837543075.

OpenAI built a large internal software product with one million lines of code and 1,500 pull requests
through an agent harness rather than by changing the underlying model. LangChain measured its own
coding agent rising from rank 30 to the top 5 on Terminal Bench 2.0 after improving the harness
without changing the model. Anthropic documented that the same model can produce either a polished
but broken application or a working app under different harness configurations. These concrete
practices and results support the source's main conclusion.

Claude Code and Codex are examples of coding-agent environments. ReAct is a prompting pattern,
not an identity to file as a person or organization.
