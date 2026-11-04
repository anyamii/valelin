class NoteNotFound(Exception): pass
def list_notes(): return {"notes":[]}
def read_note(note_id): raise NoteNotFound(note_id)
