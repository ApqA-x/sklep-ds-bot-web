import { Link, useParams } from "react-router-dom";
import { DName } from "../names";

export function UserLink({ userId, name }: { userId: string; name?: string }) {
  const { guildId = "" } = useParams();
  const label = name ? name : <DName kind="user" id={userId} />;
  return (
    <Link className="userlink" to={`/g/${guildId}/users/${userId}`} title={`Профиль: ${userId}`}>
      {label}
    </Link>
  );
}
