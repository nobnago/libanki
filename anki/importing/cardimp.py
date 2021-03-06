# -*- coding: utf-8 -*-
# Copyright: Damien Elmes <anki@ichi2.net>
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import time
from anki.lang import _
from anki.utils import fieldChecksum, ids2str
from anki.errors import *
from anki.importing.base import Importer
#from anki.deck import NEW_CARDS_RANDOM

# Stores a list of fields, tags, and optionally properties like 'ivl'
######################################################################

class ForeignCard(object):
    "An temporary object storing fields and attributes."
    def __init__(self):
        self.fields = []
        self.tags = u""

# Base class for csv/supermemo/etc importers
######################################################################

class CardImporter(Importer):

    needMapper = True
    tagDuplicates = False
    # if set, update instead of regular importing
    # (foreignCardFieldIndex, fieldModelId)
    updateKey = None
    needDelimiter = False

    def __init__(self, col, file):
        Importer.__init__(self, col, file)
        self._model = col.currentModel
        self.tagsToAdd = u""
        self._mapping = None

    def run(self):
        "Import."
        if self.updateKey is not None:
            return self.doUpdate()
        random = self.col.newCardOrder == NEW_CARDS_RANDOM
        num = 6
        if random:
            num += 1
        c = self.foreignCards()
        if self.importCards(c):
            self.col.updateCardTags(self.cardIds)
            if random:
                self.col.randomizeNewCards(self.cardIds)
        if c:
            self.col.setModified()

    def doUpdate(self):
        # grab the data from the external file
        cards = self.foreignCards()
        # grab data from db
        fields = self.col.db.all("""
select noteId, value from fields where fieldModelId = :id
and value != ''""",
                               id=self.updateKey[1])
        # hash it
        vhash = {}
        nids = []
        for (nid, val) in fields:
            nids.append(nid)
            vhash[val] = nid
        # prepare tags
        tagsIdx = None
        try:
            tagsIdx = self.mapping.index(0)
            for c in cards:
                c.tags = canonifyTags(self.tagsToAdd + " " + c.fields[tagsIdx])
        except ValueError:
            pass
        # look for matches
        upcards = []
        newcards = []
        for c in cards:
            v = c.fields[self.updateKey[0]]
            if v in vhash:
                # ignore empty keys
                if v:
                    # nid, card
                    upcards.append((vhash[v], c))
            else:
                newcards.append(c)
        # update fields
        for fm in self.model.fieldModels:
            if fm.id == self.updateKey[1]:
                # don't update key
                continue
            try:
                index = self.mapping.index(fm)
            except ValueError:
                # not mapped
                continue
            data = [{'nid': nid,
                     'fmid': fm.id,
                     'v': c.fields[index],
                     'chk': self.maybeChecksum(c.fields[index], fm.unique)}
                    for (nid, c) in upcards]
            self.col.db.execute("""
update fields set value = :v, chksum = :chk where noteId = :nid
and fieldModelId = :fmid""", data)
        # update tags
        if tagsIdx is not None:
            data = [{'nid': nid,
                     't': c.fields[tagsIdx]}
                    for (nid, c) in upcards]
            self.col.db.execute(
                "update notes set tags = :t where id = :nid",
                data)
        # rebuild caches
        cids = self.col.db.column0(
            "select id from cards where noteId in %s" %
            ids2str(nids))
        self.col.updateCardTags(cids)
        self.col.updateCardsFromNoteIds(nids)
        self.total = len(cards)
        self.col.setModified()

    def fields(self):
        "The number of fields."
        return 0

    def maybeChecksum(self, data, unique):
        if not unique:
            return ""
        return fieldChecksum(data)

    def foreignCards(self):
        "Return a list of foreign cards for importing."
        assert 0

    def resetMapping(self):
        "Reset mapping to default."
        numFields = self.fields()
        m = [f for f in self.model.fieldModels]
        m.append(0)
        rem = max(0, self.fields() - len(m))
        m += [None] * rem
        del m[numFields:]
        self._mapping = m

    def getMapping(self):
        if not self._mapping:
            self.resetMapping()
        return self._mapping

    def setMapping(self, mapping):
        self._mapping = mapping

    mapping = property(getMapping, setMapping)

    def getModel(self):
        return self._model

    def setModel(self, model):
        self._model = model
        # update the mapping for the new model
        self._mapping = None
        self.getMapping()

    model = property(getModel, setModel)

    def importCards(self, cards):
        "Convert each card into a note, apply attributes and add to col."
        # ensure all unique and required fields are mapped
        for fm in self.model.fieldModels:
            if fm.required or fm.unique:
                if fm not in self.mapping:
                    raise ImportFormatError(
                        type="missingRequiredUnique",
                        info=_("Missing required/unique field '%(field)s'") %
                        {'field': fm.name})
        active = 0
        for cm in self.model.cardModels:
            if cm.active: active += 1
        # strip invalid cards
        cards = self.stripInvalid(cards)
        cards = self.stripOrTagDupes(cards)
        self.cardIds = []
        if cards:
            self.addCards(cards)
        return cards

    def addCards(self, cards):
        "Add notes in bulk from foreign cards."
        # map tags field to attr
        try:
            idx = self.mapping.index(0)
            for c in cards:
                c.tags += " " + c.fields[idx]
        except ValueError:
            pass
        # add notes
        noteIds = [genID() for n in range(len(cards))]
        noteCreated = {}
        def fudgeCreated(d, tmp=[]):
            if not tmp:
                tmp.append(time.time())
            else:
                tmp[0] += 0.0001
            d['created'] = tmp[0]
            noteCreated[d['id']] = d['created']
            return d
        self.col.db.execute(notesTable.insert(),
            [fudgeCreated({'modelId': self.model.id,
              'tags': canonifyTags(self.tagsToAdd + " " + cards[n].tags),
              'id': noteIds[n]}) for n in range(len(cards))])
        self.col.db.execute("""
delete from notesDeleted
where noteId in (%s)""" % ",".join([str(s) for s in noteIds]))
        # add all the fields
        for fm in self.model.fieldModels:
            try:
                index = self.mapping.index(fm)
            except ValueError:
                index = None
            data = [{'noteId': noteIds[m],
                     'fieldModelId': fm.id,
                     'ordinal': fm.ordinal,
                     'id': genID(),
                     'value': (index is not None and
                               cards[m].fields[index] or u""),
                     'chksum': self.maybeChecksum(
                index is not None and
                cards[m].fields[index] or u"", fm.unique)
                     }
                    for m in range(len(cards))]
            self.col.db.execute(fieldsTable.insert(),
                                data)
        # and cards
        active = 0
        for cm in self.model.cardModels:
            if cm.active:
                active += 1
                data = [self.addMeta({
                    'id': genID(),
                    'noteId': noteIds[m],
                    'noteCreated': noteCreated[noteIds[m]],
                    'cardModelId': cm.id,
                    'ordinal': cm.ordinal,
                    'question': u"",
                    'answer': u""
                    },cards[m]) for m in range(len(cards))]
                self.col.db.execute(cardsTable.insert(),
                                    data)
        self.col.updateCardsFromNoteIds(noteIds)
        self.total = len(noteIds)

    def addMeta(self, data, card):
        "Add any scheduling metadata to cards"
        if 'fields' in card.__dict__:
            del card.fields
        t = data['noteCreated'] + data['ordinal'] * 0.00001
        data['created'] = t
        data['modified'] = t
        data['due'] = t
        data.update(card.__dict__)
        data['tags'] = u""
        self.cardIds.append(data['id'])
        data['combinedDue'] = data['due']
        if data.get('successive', 0):
            t = 1
        elif data.get('reps', 0):
            t = 0
        else:
            t = 2
        data['type'] = t
        data['queue'] = t
        return data

    def stripInvalid(self, cards):
        return [c for c in cards if self.cardIsValid(c)]

    def cardIsValid(self, card):
        fieldNum = len(card.fields)
        for n in range(len(self.mapping)):
            if self.mapping[n] and self.mapping[n].required:
                if fieldNum <= n or not card.fields[n].strip():
                    self.log.append("Note is missing field '%s': %s" %
                                    (self.mapping[n].name,
                                     ", ".join(card.fields)))
                    return False
        return True

    def stripOrTagDupes(self, cards):
        # build a cache of items
        self.uniqueCache = {}
        for field in self.mapping:
            if field and field.unique:
                self.uniqueCache[field.id] = self.getUniqueCache(field)
        return [c for c in cards if self.cardIsUnique(c)]

    def getUniqueCache(self, field):
        "Return a dict with all fields, to test for uniqueness."
        return dict(self.col.db.all(
            "select value, 1 from fields where fieldModelId = :fmid",
            fmid=field.id))

    def cardIsUnique(self, card):
        fieldsAsTags = []
        for n in range(len(self.mapping)):
            if self.mapping[n] and self.mapping[n].unique:
                if card.fields[n] in self.uniqueCache[self.mapping[n].id]:
                    if not self.tagDuplicates:
                        self.log.append("Note has duplicate '%s': %s" %
                                        (self.mapping[n].name,
                                         ", ".join(card.fields)))
                        return False
                    fieldsAsTags.append(self.mapping[n].name.replace(" ", "-"))
                else:
                    self.uniqueCache[self.mapping[n].id][card.fields[n]] = 1
        if fieldsAsTags:
            card.tags += u" Duplicate:" + (
                "+".join(fieldsAsTags))
            card.tags = canonifyTags(card.tags)
        return True
