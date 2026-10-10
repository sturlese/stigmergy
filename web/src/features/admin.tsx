import { ChannelsView } from "./access/channels";
import { GroupsView } from "./access/groups";
import { PeopleView } from "./access/people";
import { ActivityView } from "./ops/activity";
import { CapturesView } from "./ops/captures";
import { ChangesView } from "./ops/changes";
import { ContradictionsView } from "./ops/contradictions";
import { EntitiesView } from "./ops/entities";
import { GardenerView } from "./ops/gardener";
import { IndexView } from "./ops/index-health";
import { OverviewView } from "./ops/overview";

/** Administrator sections, keyed by their first route segment. */
export function AdminSection({ route }: { route: string }) {
  switch (route.split("/")[0]) {
    case "overview": return <OverviewView />;
    case "captures": return <CapturesView route={route} />;
    case "changes": return <ChangesView route={route} />;
    case "contradictions": return <ContradictionsView />;
    case "entities": return <EntitiesView />;
    case "gardener": return <GardenerView />;
    case "index": return <IndexView />;
    case "activity": return <ActivityView />;
    case "people": return <PeopleView route={route} />;
    case "groups": return <GroupsView />;
    case "channels": return <ChannelsView />;
    default: return null;
  }
}
