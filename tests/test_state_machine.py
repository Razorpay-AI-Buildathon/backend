import unittest
from app.models.case import CaseStatus, CaseStateMachine

class TestCaseStateMachine(unittest.TestCase):

    def test_valid_transitions(self):
        # DETECTED/IDENTIFIED -> ANALYZING
        self.assertTrue(CaseStateMachine.validate_transition(CaseStatus.DETECTED, CaseStatus.ANALYZING))
        self.assertTrue(CaseStateMachine.validate_transition(CaseStatus.IDENTIFIED, CaseStatus.ANALYZING))

        # ANALYZING -> ACTION_PROPOSED
        self.assertTrue(CaseStateMachine.validate_transition(CaseStatus.ANALYZING, CaseStatus.ACTION_PROPOSED))

        # ACTION_PROPOSED -> GUARD_REVIEW
        self.assertTrue(CaseStateMachine.validate_transition(CaseStatus.ACTION_PROPOSED, CaseStatus.GUARD_REVIEW))

        # GUARD_REVIEW -> APPROVED
        self.assertTrue(CaseStateMachine.validate_transition(CaseStatus.GUARD_REVIEW, CaseStatus.APPROVED))

        # APPROVED -> EXECUTING
        self.assertTrue(CaseStateMachine.validate_transition(CaseStatus.APPROVED, CaseStatus.EXECUTING))

        # EXECUTING -> FAILED or RECOVERED
        self.assertTrue(CaseStateMachine.validate_transition(CaseStatus.EXECUTING, CaseStatus.FAILED))
        self.assertTrue(CaseStateMachine.validate_transition(CaseStatus.EXECUTING, CaseStatus.RECOVERED))

        # FAILED -> ANALYZING (for replanning) or CLOSED
        self.assertTrue(CaseStateMachine.validate_transition(CaseStatus.FAILED, CaseStatus.ANALYZING))
        self.assertTrue(CaseStateMachine.validate_transition(CaseStatus.FAILED, CaseStatus.CLOSED))

        # HUMAN_REVIEW -> APPROVED or CLOSED or BLOCKED
        self.assertTrue(CaseStateMachine.validate_transition(CaseStatus.HUMAN_REVIEW, CaseStatus.APPROVED))
        self.assertTrue(CaseStateMachine.validate_transition(CaseStatus.HUMAN_REVIEW, CaseStatus.CLOSED))
        self.assertTrue(CaseStateMachine.validate_transition(CaseStatus.HUMAN_REVIEW, CaseStatus.BLOCKED))

    def test_invalid_transitions(self):
        # Terminal states must block transitions
        self.assertFalse(CaseStateMachine.validate_transition(CaseStatus.RECOVERED, CaseStatus.ANALYZING))
        self.assertFalse(CaseStateMachine.validate_transition(CaseStatus.CLOSED, CaseStatus.ANALYZING))
        self.assertFalse(CaseStateMachine.validate_transition(CaseStatus.BLOCKED, CaseStatus.GUARD_REVIEW))

        # Invalid shortcuts (e.g. bypass guard review)
        self.assertFalse(CaseStateMachine.validate_transition(CaseStatus.ACTION_PROPOSED, CaseStatus.APPROVED))
        self.assertFalse(CaseStateMachine.validate_transition(CaseStatus.IDENTIFIED, CaseStatus.EXECUTING))
        self.assertFalse(CaseStateMachine.validate_transition(CaseStatus.ANALYZING, CaseStatus.RECOVERED))

    def test_reflexive_transitions(self):
        # Current status to same status is always valid (identity transition)
        self.assertTrue(CaseStateMachine.validate_transition(CaseStatus.ANALYZING, CaseStatus.ANALYZING))
        self.assertTrue(CaseStateMachine.validate_transition(CaseStatus.RECOVERED, CaseStatus.RECOVERED))

if __name__ == "__main__":
    unittest.main()
