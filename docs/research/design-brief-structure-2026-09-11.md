# What a useful product design brief contains

Research date: 11 September 2026. Purpose: establish a sound structure before revising Corridor's designer handoff again. This is research and a proposed approach, not a new product requirement or a commissioned design scope.

## Finding

A useful brief makes the **design assignment** clear: whose problem matters, what improvement is wanted, what work is being commissioned, what limits apply, and how decisions will be made. Explaining the product is necessary, but it does not by itself tell a designer what they are being asked to do.

Design Council describes a brief as the definition of a product or service problem, with goals, constraints, budget, timing, outcomes, and risks. It synthesizes what the team has learned and directs subsequent work. Its guidance explicitly calls for understandable language and warns against rigidly specifying detailed design work. Research and background material can accompany the brief. [Design Council, *Design methods for developing services*, p. 18](https://www.designcouncil.org.uk/fileadmin/uploads/dc/Documents/DesignCouncil_Design%2520methods%2520for%2520developing%2520services.pdf)

That makes “explain every workflow, then enumerate its screens” an insufficient organizing principle. Our recommendation is to separate the short assignment from the material the designer will consult while doing it. Shortness is a usability objective here, not a universal page limit found in the sources.

## Sources and their limits

All sources below were opened and inspected. These are first-party professional methods and templates, not comparative experiments proving that one brief format produces better designs. Publication dates are reported only where the inspected source establishes them.

| Source | What it contributes | Limits |
| --- | --- | --- |
| [Design Council: service design methods](https://www.designcouncil.org.uk/fileadmin/uploads/dc/Documents/DesignCouncil_Design%2520methods%2520for%2520developing%2520services.pdf), especially p. 18 | Direct explanation of the brief's role in product/service development | Historical program guide; publication date not established from the inspected page. Not a software-screen template. |
| [Design Council: Design Buyers' Guide](https://www.designcouncil.org.uk/fileadmin/uploads/dc/Documents/Design%2520Buyers%2527%2520Guide.pdf), pp. 24–25 and 28 | Client background, objectives, staged delivery, budget and responsibilities | General design commissioning guidance, not a UX-only format. Historical price figures are not used. |
| [IDEO.org Design Kit: Frame Your Design Challenge](https://www.designkit.org/methods/frame-your-design-challenge.html) | Problem framing, audience, impact and scope | Method guidance, not a complete commercial commission. Undated page. |
| [Figma: How to create a design brief](https://www.figma.com/resource-library/how-to-create-a-design-brief/) | Practical commissioning information and outputs | Broad coverage including logos and marketing; vendor educational content. Undated page. |
| [Figma: Design brief example](https://www.figma.com/templates/design-brief-example/) | A concrete published template description | Focuses on visual and campaign work; terminology conflicts with Figma's other page. Undated page. |
| [Atlassian: Project Poster](https://www.atlassian.com/team-playbook/plays/project-poster?tab=instructions) | Problem, uncertainty and evolving project direction | A related alignment artifact, not explicitly a UX design brief. Undated page. |
| [Atlassian: Product requirements](https://www.atlassian.com/agile/product-management/requirements) | Distinguishes product behavior and downstream design material | First-party description of the company's approach; not an industry standard. Undated page. |
| [GOV.UK Service Manual: Plan user research](https://www.gov.uk/service-manual/user-research/plan-user-research-for-your-service) | Separates assumptions, research questions and research execution | Government service guidance, applied here by analogy. Published 14 March 2016; displayed update date 21 September 2018. |

## What the sources agree on—and where they differ

**Frame a problem with room to design.** IDEO.org recommends a concise challenge, an agreed intended impact, and attention to the audience's circumstances and constraints. A useful frame should admit multiple possible solutions; their test is whether the team can quickly imagine five. This supports giving a designer the work users need to accomplish without declaring every navigation choice in advance. [IDEO.org](https://www.designkit.org/methods/frame-your-design-challenge.html)

**Specify the commission.** Design Council's commissioning guide asks for business and customer context, objectives, deliverables, timescales, budget, and client responsibilities. It recommends a concise brief with additional information attached or linked and expects the designer to challenge and refine it. [Design Council, *Design Buyers' Guide*, pp. 24–25 and 28](https://www.designcouncil.org.uk/fileadmin/uploads/dc/Documents/Design%2520Buyers%2527%2520Guide.pdf)

Figma includes a project owner, overview, goals, problem, audience, deliverables, budget, timeline and relevant existing design guidance. It treats development of the brief as collaborative. Its examples also emphasize brand, competitors and visual direction. These can matter, but copying every branding heading into an operational software brief would not establish the user's work or the assignment. [Figma guidance](https://www.figma.com/resource-library/how-to-create-a-design-brief/)

**Names are not standardized.** Figma's guidance distinguishes a design brief from a broader creative brief; its template page treats those names as equivalent and describes campaign goals, visual assets, tone and brand guidance. This is a real inconsistency, not a reason to choose whichever definition supports a preferred checklist. Specify the artifact's purpose and audience. [Figma guidance](https://www.figma.com/resource-library/how-to-create-a-design-brief/), [Figma template](https://www.figma.com/templates/design-brief-example/)

**Agreement need not freeze learning.** Design Council allows sponsor sign-off. Atlassian's Project Poster evolves through three stages: problem, validation, and readiness to make. It can link to other artifacts rather than contain everything. Our synthesis is to agree the assignment, responsibilities and limits while keeping assumptions and design proposals open to revision. [Design Council, p. 18](https://www.designcouncil.org.uk/fileadmin/uploads/dc/Documents/DesignCouncil_Design%2520methods%2520for%2520developing%2520services.pdf), [Atlassian Project Poster](https://www.atlassian.com/team-playbook/plays/project-poster?tab=instructions)

## Keep different documents' jobs clear

These are practical distinctions for this handoff, not mandatory industry naming rules:

| Material | Its main question | Treatment in the handoff |
| --- | --- | --- |
| Product design brief | What design work are we asking for, why, and within what limits? | Main document. |
| Product explanation | What does Corridor do, and why would someone use it? | A short opening supported by one understandable example. |
| Roadmap | What is planned, sequenced, implemented or still uncertain? | Reference for selecting and checking the assignment's scope. |
| Product requirements | What must the product do? | Link relevant requirements; summarize only constraints that materially affect design. |
| Research plan | What must we learn, from whom, and how? | Name key questions in the brief; keep recruitment and study logistics separately. |
| Wireframes and interaction specification | How will the chosen experience behave? | Design work or supporting material, depending on what already exists. |
| Coverage and acceptance checklist | Which agreed behaviors or deliverable conditions have been checked? | Supporting reference; not the main explanation of the problem. |

Atlassian's PRD guidance addresses purpose, features and behavior. It includes assumptions, stories, open questions and exclusions, and links explorations and wireframes after solutions develop. It also recommends linking supporting detail and warns against fully prescribing a project before collaboration. Thus a PRD need not be an exhaustive specification either; these artifacts overlap. [Atlassian product requirements](https://www.atlassian.com/agile/product-management/requirements)

GOV.UK's research guidance calls for questions, user groups, methods and recruitment, updated as learning progresses. It explicitly says to turn unsupported assumptions into research questions. A fictional walkthrough can demonstrate a proposed experience, but cannot establish that actual coordinators behave that way. A performance target similarly needs a measurement plan before it can become a demonstrated result. [GOV.UK research planning](https://www.gov.uk/service-manual/user-research/plan-user-research-for-your-service)

## Recommended structure for Corridor

The following is our synthesis for this situation. The sequence and section count are recommendations, not a template prescribed by any one source. Draft it around the next design engagement, then add supporting material only when it helps a specific decision.

1. **The assignment.** State what the designer is being asked to investigate or produce, the product's current stage, and the decision this work will enable. Distinguish redesigning an existing experience from exploring an unbuilt one. Name the person responsible for the brief.
2. **The product and the problem.** Explain in ordinary language who uses Corridor, what work they already do, what makes it difficult, and what Corridor is intended to improve. Give enough context to understand the assignment without reading the roadmap.
3. **Users and what we know.** Identify the primary user and relevant collaborators. Describe their responsibilities, working conditions and existing tools using available evidence. Separate observed findings, product assumptions and fictional examples. Link real research or state that it is missing.
4. **The improvement we want.** Identify the highest-priority user and business outcomes. Say how a proposed design will be assessed. Distinguish eventual pilot outcomes from what a prototype review can establish.
5. **The scope of this engagement.** Select the work to address first, its starting point and its completion condition. List explicit exclusions. Explain dependencies and what can be represented in a prototype. Do not silently turn the whole roadmap into the designer's assignment.
6. **Constraints and room to decide.** Summarize applicable product rules, technical or accessibility requirements, existing assets and operational limitations. Separately identify choices the designer should explore: navigation, grouping, page boundaries, information hierarchy and interaction patterns. Mark suggestions as suggestions.
7. **Outputs and working agreement.** Specify required artifacts, level of finish, priority journeys and variations, editable files, and handoff expectations. Name the decision maker, contributors, feedback process, review points, timing and available resources. Use “to be agreed” for missing commercial facts; do not invent them.
8. **Inputs and unresolved questions.** Provide a short, ordered reading list and identify missing information, who will resolve it, and which design decisions it affects. Keep this list current.

This ordering lets a designer determine what job they are taking on before absorbing detailed examples. It also exposes missing inputs instead of covering them with more explanation.

## What should accompany the brief

For this project, a supporting folder or appendix could contain:

- A small consistent example set: source spreadsheet, incoming message, proposed change, unresolved question and reporting output. Label fictional material visibly.
- One current-workflow illustration and one proposed journey. Distinguish evidence about present practice from a product hypothesis.
- Relevant screenshots or a usable product walkthrough, with a clear account of what actually works today.
- Selected product rules expressed through their user-facing consequences, with links to authoritative project decisions.
- A provisional inventory of tasks, states and candidate screens; let the designer propose how those fit together.
- Detailed test cases, failure states, permission rules and source references needed during design and handoff.

These are proposed inputs for Corridor, not claims that they all exist. An appendix should earn its place by helping the designer make or check a decision. A long inventory can support estimation, but cannot validate “15 screens” as the correct architecture.

## Test whether the next draft is ready

Before issuing it, ask a designer unfamiliar with the project to explain the assignment back: the user, the current difficulty, the intended improvement, the first scope, the deliverables, the constraints, the choices they own and who decides. Record where they still have to guess. This is a proposed review method, not a published universal brief standard.

The next rewrite should address those gaps before expanding screen descriptions. If the primary uncertainty is the user's actual work, commission discovery. If that work is understood but the organization of the interface is unresolved, commission interaction exploration. If the experience is agreed, commission detailed interface design and handoff. Those are materially different jobs.

## Comparison with the current Corridor document

The following is local analysis of [Corridor_Product_and_Design_Brief.docx](/Users/bryceharmon/Downloads/Corridor_Product_and_Design_Brief.docx), based on direct inspection of its text and rendered pages. These observations concern that artifact, not claims made by the external sources.

| What the current document contains | What remains unclear for a designer |
| --- | --- |
| Product explanation and broad assignment, p. 1 | Which commission is this: discovery, interaction exploration, visual design, or a build-ready handoff? |
| User description and pilot targets, p. 2 | Which statements come from actual customer observation? No customer research or working documents are linked. |
| Clearly labeled fictional scenario and prescribed walkthrough, pp. 4–10 | Useful illustration, but it cannot supply missing customer evidence. The proposed interaction occupies much of the main brief before the commission is settled. |
| Recommended first handoff, p. 12 | Deliverables exist, but their finish, organization, component/state coverage and acceptance are not agreed. |
| Usability tasks, targets and evidence still needed, p. 13 | Useful material, but the brief needs a consolidated account of what is known, what needs research and who will provide it. |
| Supporting constraints, 41 workflows and 18 scenarios, pp. 14–19 | These support later coverage checks. They do not establish first-phase priority or the assignment's size. |

The document explains product approval permissions, but omits the design decision owner, feedback process, schedule and budget or available capacity. It also lacks an inventory of existing interface work, design assets and brand constraints. Adding more screen instructions would not resolve those gaps.

Keep the useful product explanation, illustrative material and constraints. Reorganize them into a main brief plus a linked product walkthrough and reference material. The earlier conversational estimate of “15 screens” was a provisional decomposition, not researched information architecture or approved scope. Neither that count nor the current document's 20 pages should become a requirement for the next draft.
