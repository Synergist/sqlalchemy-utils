from collections import defaultdict
import six
import sqlalchemy as sa
from sqlalchemy.orm import RelationshipProperty
from sqlalchemy.orm.attributes import (
    set_committed_value, InstrumentedAttribute
)
from sqlalchemy.orm.session import object_session


class with_backrefs(object):
    """
    Marks given attribute path so that whenever its fetched with batch_fetch
    the backref relations are force set too.
    """
    def __init__(self, path):
        self.path = path


class Path(object):
    """
    A class that represents an attribute path.
    """
    def __init__(self, entities, prop, populate_backrefs=False):
        self.property = prop
        self.entities = entities
        self.populate_backrefs = populate_backrefs
        if not isinstance(self.property, RelationshipProperty):
            raise Exception(
                'Given attribute is not a relationship property.'
            )
        self.fetcher = self.fetcher_class(self)

    @property
    def session(self):
        return object_session(self.entities[0])

    @property
    def parent_model(self):
        return self.entities[0].__class__

    @property
    def model(self):
        return self.property.mapper.class_

    @classmethod
    def parse(cls, entities, path, populate_backrefs=False):
        if isinstance(path, six.string_types):
            attrs = path.split('.')

            if len(attrs) > 1:
                related_entities = []
                for entity in entities:
                    related_entities.extend(getattr(entity, attrs[0]))

                subpath = '.'.join(attrs[1:])
                return Path.parse(related_entities, subpath, populate_backrefs)
            else:
                attr = getattr(
                    entities[0].__class__, attrs[0]
                )
        elif isinstance(path, InstrumentedAttribute):
            attr = path
        else:
            raise Exception('Unknown path type.')

        return Path(entities, attr.property, populate_backrefs)

    @property
    def fetcher_class(self):
        if self.property.secondary is not None:
            return ManyToManyFetcher
        else:
            if self.property.direction.name == 'MANYTOONE':
                return ManyToOneFetcher
            else:
                return OneToManyFetcher


class CompositePath(object):
    def __init__(self, *paths):
        self.paths = paths


def batch_fetch(entities, *attr_paths):
    """
    Batch fetch given relationship attribute for collection of entities.

    This function is in many cases a valid alternative for SQLAlchemy's
    subqueryload and performs lot better.

    :param entities: list of entities of the same type
    :param attr_paths:
        List of either InstrumentedAttribute objects or a strings representing
        the name of the instrumented attribute

    Example::


        from sqlalchemy_utils import batch_fetch


        users = session.query(User).limit(20).all()

        batch_fetch(users, User.phonenumbers)


    Function also accepts strings as attribute names: ::


        users = session.query(User).limit(20).all()

        batch_fetch(users, 'phonenumbers')


    Multiple attributes may be provided: ::


        clubs = session.query(Club).limit(20).all()

        batch_fetch(
            clubs,
            'teams',
            'teams.players',
            'teams.players.user_groups'
        )

    You can also force populate backrefs: ::


        from sqlalchemy_utils import with_backrefs


        clubs = session.query(Club).limit(20).all()

        batch_fetch(
            clubs,
            'teams',
            'teams.players',
            with_backrefs('teams.players.user_groups')
        )

    """

    if entities:
        fetcher = FetchingCoordinator()
        for attr_path in attr_paths:
            fetcher(entities, attr_path)


class FetchingCoordinator(object):
    def __call__(self, entities, path):
        populate_backrefs = False
        if isinstance(path, with_backrefs):
            path = path.path
            populate_backrefs = True

        if isinstance(path, CompositePath):
            fetchers = []
            for path in path.paths:
                fetchers.append(
                    Path.parse(entities, path, populate_backrefs).fetcher
                )

            fetcher = CompositeFetcher(*fetchers)
        else:
            fetcher = Path.parse(entities, path, populate_backrefs).fetcher
        fetcher.fetch()
        fetcher.populate()


class CompositeFetcher(object):
    def __init__(self, *fetchers):
        if not all(
            fetchers[0].path.model == fetcher.path.model
            for fetcher in fetchers
        ):
            raise Exception(
                'Each relationship property must have the same class when '
                'using CompositeFetcher.'
            )
        self.fetchers = fetchers

    @property
    def session(self):
        return self.fetchers[0].path.session

    @property
    def model(self):
        return self.fetchers[0].path.model

    @property
    def condition(self):
        return sa.or_(
            *[fetcher.condition for fetcher in self.fetchers]
        )

    @property
    def related_entities(self):
        return self.session.query(self.model).filter(self.condition)

    def fetch(self):
        for entity in self.related_entities:
            for fetcher in self.fetchers:
                if getattr(entity, fetcher.remote_column_name) is not None:
                    fetcher.append_entity(entity)

    def populate(self):
        for fetcher in self.fetchers:
            fetcher.populate()


class Fetcher(object):
    def __init__(self, path):
        self.path = path
        self.prop = self.path.property
        self.parent_dict = defaultdict(list)

    @property
    def local_values_list(self):
        return [
            self.local_values(entity)
            for entity in self.path.entities
        ]

    @property
    def related_entities(self):
        return self.path.session.query(self.path.model).filter(self.condition)

    @property
    def remote_column_name(self):
        return list(self.path.property.remote_side)[0].name

    def local_values(self, entity):
        return getattr(entity, list(self.prop.local_columns)[0].name)

    def populate_backrefs(self, related_entities):
        """
        Populates backrefs for given related entities.
        """
        backref_dict = dict(
            (self.local_values(entity), [])
            for entity, parent_id in related_entities
        )
        for entity, parent_id in related_entities:
            backref_dict[self.local_values(entity)].append(
                self.path.session.query(self.path.parent_model).get(parent_id)
            )
        for entity, parent_id in related_entities:
            set_committed_value(
                entity,
                self.prop.back_populates,
                backref_dict[self.local_values(entity)]
            )

    def populate(self):
        """
        Populate batch fetched entities to parent objects.
        """
        for entity in self.path.entities:
            set_committed_value(
                entity,
                self.prop.key,
                self.parent_dict[self.local_values(entity)]
            )

        if self.path.populate_backrefs:
            self.populate_backrefs(self.related_entities)

    @property
    def condition(self):
        return getattr(self.path.model, self.remote_column_name).in_(
            self.local_values_list
        )

    def fetch(self):
        for entity in self.related_entities:
            self.append_entity(entity)


class ManyToManyFetcher(Fetcher):
    @property
    def remote_column_name(self):
        for column in self.prop.remote_side:
            for fk in column.foreign_keys:
                # TODO: make this support inherited tables
                if fk.column.table == self.path.parent_model.__table__:
                    return fk.parent.name

    @property
    def related_entities(self):
        return (
            self.path.session
            .query(
                self.path.model,
                getattr(self.prop.secondary.c, self.remote_column_name)
            )
            .join(
                self.prop.secondary, self.prop.secondaryjoin
            )
            .filter(
                getattr(self.prop.secondary.c, self.remote_column_name).in_(
                    self.local_values_list
                )
            )
        )

    def fetch(self):
        for entity, parent_id in self.related_entities:
            self.parent_dict[parent_id].append(
                entity
            )


class ManyToOneFetcher(Fetcher):
    def __init__(self, path):
        Fetcher.__init__(self, path)
        self.parent_dict = defaultdict(lambda: None)

    def append_entity(self, entity):
        self.parent_dict[getattr(entity, self.remote_column_name)] = entity


class OneToManyFetcher(Fetcher):
    def append_entity(self, entity):
        self.parent_dict[getattr(entity, self.remote_column_name)].append(
            entity
        )