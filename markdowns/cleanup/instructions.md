I recently built my mvp based on markdowns/MVP/cleaned/plan.md


This new plan should be about the post-engineering cleanup. Make a plan based on these elements:


    - Find and remove any leftovers from poc that are juste bridges
    - Cleanup comments and references to milestones, steps, plan etc in the code or comments. The code should just describe its current state not what was before and what might come after
    - Simplify code comments where necessary - sometimes they reflect agent thoughts
    - Do a thorough security check against my repository.
    - Simplify code in general where it is safe to do so (similar to the /simplify command). Essentially re-analyse the entire repository to see if now that everything is complete there are any things that make no sense anymore. Take into account the results of the security check in the previous step.
    - Fix my github CI steps that currently fail. Remove what is useless, add what would make sense to add.
    - Remove the « markdown » folder as it contains a lot of historical obsolete information.
    - Add one readme per package, detailing the internals.
    - Update high quality readme at repository level. It should serve as manual an be readable like a book for someone learning the topic. It should also contain a « getting started » and explain what the project can do and cannot do yet.


Make a markdown plan that can be saved for later and implemented step by step by my coding agent.