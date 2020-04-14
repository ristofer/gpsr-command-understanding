import copy
import re
import sys
from collections import defaultdict
from string import printable

import importlib_resources
from lark import Lark, Tree, exceptions

from gpsr_command_understanding.generator.generation import generate_sentence_parse_pairs, \
    expand_pair_full
from gpsr_command_understanding.generator.grammar import TypeConverter, expand_shorthand, RemovePrefix, CompactUnderscorePrefixed
from gpsr_command_understanding.util import get_wildcards_forest, has_placeholders, replace_child_in_tree, \
    get_wildcards
from gpsr_command_understanding.generator.grammar import tree_printer

try:
    from itertools import izip_longest as zip_longest
except ImportError:
    from itertools import zip_longest

GENERATOR_GRAMMARS = {2018: importlib_resources.read_text("gpsr_command_understanding.resources", "generator.lark"),
                      2019: importlib_resources.read_text("gpsr_command_understanding.resources", "generator.lark")}

SEMANTIC_FORMS = {"lambda": importlib_resources.read_text("gpsr_command_understanding.resources", "lambda_ebnf.lark")}


class LambdaParserWrapper:
    def __init__(self, grammar_spec=SEMANTIC_FORMS["lambda"]):
        # FIXME: Ensure that the import statement will work in different contexts
        # This grammar uses an import statement, which will trigger a local search for the imported file.
        # We aren't guaranteed that resources live as files (could be zipped up), so this will
        # probably break for distribution.
        with importlib_resources.path("gpsr_command_understanding.resources", "generator.lark") as path:
            # When a grammar comes in as a string, lark will check where the main script is located
            # to start its search. We'll manually point it to a path that importlib tells us has
            # the imported grammar.
            old_main = sys.modules['__main__'].__file__
            sys.modules['__main__'].__file__ = path
            self.parser = Lark(grammar_spec,
                               start='start', parser="lalr")
            # Because the imported rules come into a namespace, we'll have to run our own clean up, but then
            # it's as though we cut and pasted the imported rules
            self.post_process = RemovePrefix("generator__")
            self.compact = TypeConverter() * CompactUnderscorePrefixed()
            # Clean up
            sys.modules['__main__'].__file__ = old_main

    def parse(self, to_parse):
        parsed = self.parser.parse(to_parse)
        de_namespaced = self.post_process.visit(parsed)
        compacted_and_typed = self.compact.transform(de_namespaced)
        return compacted_and_typed


# TODO(nickswalker): Document these methods
# TODO(nickswalker): Add grounding methods
class Generator:
    def __init__(self, grammar_format_version=2018, semantic_form_version="lambda"):
        grammar_spec = GENERATOR_GRAMMARS[grammar_format_version]
        annotation_spec = SEMANTIC_FORMS[semantic_form_version]
        self.grammar_parser = Lark(grammar_spec,
                                   start='rule_start', parser="lalr", transformer=TypeConverter())
        self.sequence_parser = Lark(grammar_spec,
                                    start='expression_start', parser="lalr", transformer=TypeConverter())
        self.lambda_parser = LambdaParserWrapper()
        self.semantic_form_version = semantic_form_version
        self.rules = {}
        self.semantics = {}

    def parse_production_rule(self, line, expand=True):
        try:
            parsed = self.grammar_parser.parse(line)
        except exceptions.LarkError as e:
            raise e

        if len(parsed.children) == 0:
            return None, []

        rhs_list_expanded = [parsed.children[1]]
        if expand:
            rhs_list_expanded = expand_shorthand(parsed.children[1])
        #print(parsed.pretty())
        return parsed.children[0], rhs_list_expanded

    def load_rules(self, grammar_files, expand_shorthand=True):
        """
        :param grammar_files: list of files
        :return: dictionary with NonTerminal key and values for all productions
        """
        if not isinstance(grammar_files, list):
            grammar_files = [grammar_files]

        i = 0
        for grammar_file in grammar_files:
            for line in grammar_file:
                # Scrub out any non-printable characters; some grammar files have annoying byte order
                # markers attached
                line = line.strip()
                line = re.sub("[^{}]+".format(printable), "", line)
                # parse into possible productions
                lhs, rhs_productions = self.parse_production_rule(line, expand_shorthand)
                # Skip emtpy LHS (comments)
                # add to dictionary, if already there then append to list of rules
                # using set to avoid duplicates
                if not lhs:
                    continue
                elif lhs not in self.rules:
                    self.rules[lhs] = rhs_productions
                    i += 1
                else:
                    self.rules[lhs].extend(rhs_productions)
                    i += 1
        return i

    def __parse_rule(self, line, rule_dict):
        # Probably a comment line
        if "=" not in line:
            return 0
        # TODO: Properly compose these grammars so that we don't have to manually interface them
        prod, semantics = line.split("=")
        try:
            prod = self.sequence_parser.parse(prod.strip())
        except exceptions.LarkError as e:
            print(prod)
            print(e)
            raise e

        # Probably a comment
        if len(prod.children) == 0:
            return 0

        expanded_prod_heads = expand_shorthand(prod)
        sem = semantics.strip()

        try:
            sem = self.lambda_parser.parse(sem)
        except exceptions.LarkError as e:
            print(sem)
            print(e)
            raise e

        i = 0
        expanded_sem_heads = expand_shorthand(sem)
        for prod, sem in zip_longest(expanded_prod_heads, expanded_sem_heads, fillvalue=expanded_sem_heads[0]):
            # Check for any obvious errors in the annotation
            prod_wildcards = get_wildcards_forest([prod])
            sem_wildcards = get_wildcards_forest([sem]) if isinstance(sem, Tree) else set()

            if sem_wildcards.difference(prod_wildcards):
                raise RuntimeError(
                    "Semantics rely on non-terminal {} that doesn't occur in rule: {}".format(sem_wildcards, line))

            rule_dict[prod] = sem
            i += 1
        return i

    def load_semantics_rules(self, semantics_files):
        """
        :param semantics_files:
        :return: dictionary mapping productions in grammar to semantics for planner
        """

        if not isinstance(semantics_files, list):
            semantics_files = [semantics_files]
        i = 0
        for semantics_file in semantics_files:
            for line in semantics_file:
                cleaned = line.strip()
                i += self.__parse_rule(cleaned, self.semantics)

        return i

    def ground(self, tree, random_source=None):
        return next(self.generate_groundings(tree, random_source=random_source))

    def generate_groundings(self, tree, random_source=None):
        wildcards = get_wildcards(tree)
        assignment = {}

        constraints = defaultdict(set)
        for wildcard in wildcards:
            # Could be another instance of the same wildcard
            if wildcard.id:
                constraints[wildcard] = set()
                if wildcard in assignment:
                    continue
                for other_wildcard, item_constraints in constraints.items():
                    # Any wildcard of the same name with a different ID needs to be different
                    if other_wildcard.name == wildcard.name and other_wildcard.id != wildcard.id:
                        constraints[wildcard].add(other_wildcard)

        yield from self.__populate_with_constraints(tree, constraints)

    def __populate_with_constraints(self, tree, constraints):
        wildcards = get_wildcards(tree)
        if not wildcards:
            yield tree
            return
        wildcard = next(wildcards)
        item_constraints = constraints[wildcard]
        candidates = self.knowledge_base.by_name[wildcard.name]

        for candidate in candidates:
            if isinstance(item_constraints, set):
                valid = True
                for constraint in item_constraints:
                    # Constraints only point backwards, so this constraint is saying that current wildcard
                    # must be different from a previously fixed value
                    if candidate == constraints[constraint]:
                        valid = False
                if not valid:
                    continue
            else:
                # We should have already replaced this wildcard if it has a fixed constraint
                assert False
            fixed = copy.deepcopy(constraints)
            fresh_tree = copy.deepcopy(tree)
            fixed[wildcard].clear()
            fixed[wildcard] = candidate
            replace_child_in_tree(fresh_tree, wildcard, candidate)
            yield from self.__populate_with_constraints(fresh_tree, fixed)

    def _print_semantics_rules(self):
        for key, expansion in self.semantics.items():
            print(tree_printer(key))
            print(tree_printer(expansion))
            print("----------------")


def get_grounding_per_each_parse(generator, random_source):
    grounded_examples = {}

    for rules, rules_anon, rules_ground, semantics in generator:
        # Start with each rule, since this is guaranteed to get at least all possible parses
        # Note, this may include parses that don't fall in the grammar...
        for generation_path, semantic_production in semantics.items():
            # Some non-terminals may expand into different parses (like $oprop)! So we'll expand them
            # every which way
            wild_expansions = list(generate_sentence_parse_pairs(generation_path, rules, semantics,
                                                                 yield_requires_semantics=True,
                                                                 random_generator=random_source))
            # We're going to be throwing away expansions that have the same parse, so let's
            # randomize here to make sure we aren't favoring the last expansion.
            # Note that the above generation should also return expansions in a random order anyway
            random_source.shuffle(wild_expansions)

            for utterance_wild, parse_wild in list(wild_expansions):
                utterance_anon, parse_anon = next(expand_pair_full(utterance_wild, parse_wild, rules_anon, branch_cap=1,
                                                                   random_generator=random_source))

                utterance, parse_ground = next(expand_pair_full(utterance_wild, parse_wild, rules_ground, branch_cap=1,
                                                                random_generator=random_source))
                assert not has_placeholders(utterance)
                assert not has_placeholders(parse_ground)
                assert not has_placeholders(parse_ground)
                # We expect this to happen sometimes because of the cat1 cat2 object known wildcard situation
                if has_placeholders(parse_anon):
                    continue

                grounded_examples[parse_anon] = (utterance, parse_anon, parse_ground)

    return list(grounded_examples.values())