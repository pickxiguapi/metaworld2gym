import os
from pathlib import Path

import numpy as np
import tqdm
import zarr
from termcolor import cprint

from metaworld.policies import *  # noqa: F403

from .metaworld_env import MetaWorldEnv

TASK_DESCRIPTION = {
    "basketball": "Dunk the basketball into the basket.",
    "bin-picking": "Grasp the puck from one bin and place it into another bin.",
    "button-press": "Press a button.",
    "button-press-topdown": "Press a button from the top.",
    "button-press-topdown-wall": "Bypass a wall and press a button from the top.",
    "button-press-wall": "Bypass a wall and press a button.",
    "coffee-button": "Push a button on the coffee machine.",
    "coffee-pull": "Pull a mug from a coffee machine.",
    "coffee-push": "Push a mug under a coffee machine.",
    "dial-turn": "Rotate a dial 180 degrees.",
    "disassemble": "Pick a nut out of the peg.",
    "door-lock": "Lock the door by rotating the lock clockwise.",
    "door-open": "Open a door with a revolving joint.",
    "door-unlock": "Unlock the door by rotating the lock counter-clockwise.",
    "drawer-close": "Push and close a drawer.",
    "drawer-open": "Open a drawer.",
    "faucet-close": "Rotate the faucet clockwise.",
    "faucet-open": "Rotate the faucet counter-clockwise.",
    "hammer": "Hammer a screw on the wall.",
    "handle-press": "Press a handle down.",
    "handle-press-side": "Press a handle down sideways.",
    "handle-pull": "Pull a handle up.",
    "handle-pull-side": "Pull a handle up sideways.",
    "shelf-place": "Pick and place a puck onto a shelf.",
    "soccer": "Kick a soccer into the goal.",
    "stick-push": "Grasp a stick and push a box using the stick.",
    "sweep": "Sweep a puck off the table.",
    "sweep-into": "Sweep a puck into a hole.",
    "window-close": "Push and close a window.",
    "window-open": "Push and open a window.",
}
TASK_DESCRIPTION = [
    {"task_name": task, "task_id": i, "lang_instr": desc} for i, (task, desc) in enumerate(TASK_DESCRIPTION.items())
]


def load_mw_policy(task_name: str):
    if task_name == "peg-insert-side":
        agent = SawyerPegInsertionSideV2Policy()  # noqa: F405
    else:
        task_name = task_name.split("-")
        task_name = [s.capitalize() for s in task_name]
        task_name = "Sawyer" + "".join(task_name) + "V2Policy"
        agent = eval(task_name)()
    return agent


def collect_dataset(
    num_episodes: int = 100,
    root_dir: str = "data",
    render_device: str = "cuda:0",
    num_point_clouds: int = 512,
    image_size: int = 224,
    chunk_size: int = 10,
):
    save_dir = Path(root_dir)
    os.makedirs(save_dir, exist_ok=True)

    begin_idx = []
    end_idx = []
    lang_instr = []
    compressor = zarr.Blosc(cname="zstd", clevel=3, shuffle=1)
    total_size = 0

    dataset = zarr.open(save_dir / "metaworld.zarr", mode="w")
    data = dataset.create_group("data")
    meta = dataset.create_group("meta")

    data.create_dataset(
        "image",
        shape=(0, 3, image_size, image_size),
        dtype=np.uint8,
        chunks=(chunk_size, 3, image_size, image_size),
        compressor=compressor,
    )
    data.create_dataset(
        "depth",
        shape=(0, image_size, image_size),
        dtype=np.float32,
        chunks=(chunk_size, image_size, image_size),
        compressor=compressor,
    )
    data.create_dataset(
        "point_cloud",
        shape=(0, num_point_clouds, 6),
        dtype=np.float32,
        chunks=(chunk_size, num_point_clouds, 6),
        compressor=compressor,
    )
    data.create_dataset("agent_pos", shape=(0, 9), dtype=np.float32, chunks=(chunk_size, 9), compressor=compressor)
    data.create_dataset("full_state", shape=(0, 39), dtype=np.float32, chunks=(chunk_size, 39), compressor=compressor)
    data.create_dataset("action", shape=(0, 4), dtype=np.float32, chunks=(chunk_size, 4), compressor=compressor)
    data.create_dataset("task_id", shape=(0,), dtype=np.int64, compressor=compressor)
    meta.create_dataset("episode_ends", shape=(0,), dtype=np.int64, compressor=compressor)

    for i, task in enumerate(TASK_DESCRIPTION):
        env = MetaWorldEnv(
            task_name=task["task_name"],
            render_device=render_device,
            use_point_crop=True,
            num_points=num_point_clouds,
            image_size=image_size,
        )

        cprint(f"Task name: {task['task_name']}, Number of episodes: {num_episodes}", "green")

        image_ds = np.empty((num_episodes * 200, 3, image_size, image_size), dtype=np.uint8)
        depth_ds = np.empty((num_episodes * 200, image_size, image_size), dtype=np.float32)
        pc_ds = np.empty((num_episodes * 200, num_point_clouds, 6), dtype=np.float32)
        agent_pos_ds = np.empty((num_episodes * 200, 9), dtype=np.float32)
        full_state_ds = np.empty((num_episodes * 200, 39), dtype=np.float32)
        action_ds = np.empty((num_episodes * 200, 4), dtype=np.float32)
        episode_ends_ds = []
        task_id_ds = np.full((num_episodes * 200), fill_value=task["task_id"], dtype=np.int64)

        episode_idx, ptr = 0, 0
        mw_policy = load_mw_policy(task["task_name"])

        for episode_idx in tqdm.tqdm(range(num_episodes)):
            obs = env.reset()

            done, ep_reward, ep_success, ep_success_times = False, 0.0, False, 0

            while not done:
                image_ds[ptr] = obs["image"].astype(np.uint8)
                depth_ds[ptr] = obs["depth"].astype(np.float32)
                pc = obs["point_cloud"].astype(np.float32)
                pc[:, 3:] = pc[:, 3:] / 255.0
                pc_ds[ptr] = pc
                agent_pos_ds[ptr] = obs["agent_pos"].astype(np.float32)
                full_state_ds[ptr] = obs["full_state"].astype(np.float32)
                action_ds[ptr] = mw_policy.get_action(obs["full_state"])

                obs, reward, done, info = env.step(action_ds[ptr])
                ep_reward += reward
                ep_success = ep_success or info["success"]
                ep_success_times += info["success"]
                ptr += 1

                if done or ep_success_times >= 20:
                    break

            if not ep_success or ep_success_times < 1:
                cprint(
                    f"Episode: {episode_idx} failed with reward {ep_reward} and success times {ep_success_times}", "red"
                )
            else:
                episode_ends_ds.append(ptr)
                cprint(
                    f"Episode: {episode_idx + 1} / {num_episodes}, Reward: {ep_reward}, Success Times: {ep_success_times}",
                    "green",
                )

        begin_idx.append(0 if len(begin_idx) == 0 else end_idx[-1])
        end_idx.append(ptr if len(end_idx) == 0 else end_idx[-1] + ptr)
        lang_instr.append(task["lang_instr"])

        size = end_idx[-1] - begin_idx[-1]
        data["image"].append(image_ds[:size])
        data["depth"].append(depth_ds[:size])
        data["point_cloud"].append(pc_ds[:size])
        data["agent_pos"].append(agent_pos_ds[:size])
        data["full_state"].append(full_state_ds[:size])
        data["action"].append(action_ds[:size])
        data["task_id"].append(task_id_ds[:size])
        meta["episode_ends"].append(np.array(episode_ends_ds) + total_size)
        total_size += size

    meta.attrs["lang_instr"] = [{lang: [begin, end]} for lang, begin, end in zip(lang_instr, begin_idx, end_idx)]


if __name__ == "__main__":
    collect_dataset(
        num_episodes=100,
        root_dir="data",
        render_device="cuda:0",
        num_point_clouds=1024,
        image_size=224,
    )
