"""Display labels and suggested tags; stored values remain the Z0 values."""

from zymptom.domain import EpisodeOutcome, HelpLevel, ResumeAbility

OUTCOMES = {
    EpisodeOutcome.COMPLETED_NORMALLY: "Completed normally",
    EpisodeOutcome.COMPLETED_WITH_DIFFICULTY: "Completed with difficulty",
    EpisodeOutcome.COMPLETED_WITH_ADAPTATION: "Completed with adaptation",
    EpisodeOutcome.COMPLETED_WITH_HELP: "Completed with help",
    EpisodeOutcome.STOPPED_EARLY: "Stopped early",
    EpisodeOutcome.UNABLE_TO_COMPLETE: "Unable to complete",
    EpisodeOutcome.AVOIDED_DUE_TO_LIMITATION: "Avoided because of limitation",
}
HELP_LEVELS = {
    HelpLevel.NONE: "No help",
    HelpLevel.SOME: "Some help",
    HelpLevel.SIGNIFICANT: "Significant help",
    HelpLevel.OTHER_PERSON_COMPLETED_PART: "Someone else completed part",
    HelpLevel.OTHER_PERSON_COMPLETED_ALL: "Someone else completed all",
}
RESUME_ABILITIES = {
    ResumeAbility.IMMEDIATELY: "Immediately",
    ResumeAbility.AFTER_SHORT_BREAK: "After a short break",
    ResumeAbility.LATER_SAME_DAY: "Later the same day",
    ResumeAbility.NEXT_DAY: "Next day",
    ResumeAbility.COULD_NOT_RESUME: "Could not resume",
    ResumeAbility.UNKNOWN: "Unknown",
}
FUNCTIONAL_DEMANDS = {
    "sitting": "Sitting", "standing": "Standing", "walking": "Walking",
    "lifting_carrying": "Lifting/carrying", "reaching": "Reaching", "bending": "Bending",
    "fine_motor": "Fine motor", "concentration": "Concentration", "memory": "Memory",
    "social_interaction": "Social interaction", "sensory_tolerance": "Sensory tolerance",
}
SYMPTOMS = {
    "pain": "Pain", "fatigue": "Fatigue", "weakness": "Weakness",
    "numbness_tingling": "Numbness/tingling", "dizziness": "Dizziness",
    "cognitive_difficulty": "Cognitive difficulty", "sensory_overload": "Sensory overload",
    "shortness_of_breath": "Shortness of breath", "emotional_dysregulation": "Emotional dysregulation",
}
ADAPTATIONS = {
    "changed_position": "Changed position", "sat_down": "Sat down",
    "took_break": "Took break", "needed_help": "Needed help",
}
RECOVERY_ACTIONS = {
    "lay_down": "Lay down", "medication": "Medication", "sensory_recovery": "Sensory recovery",
}
CONSEQUENCES = {
    "needed_rest": "Needed rest", "missed_next_activity": "Missed next activity",
    "reduced_activity": "Reduced activity afterward",
}
